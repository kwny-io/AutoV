"""S43 交付包测试。方案 §9 的 E37 / E27 / E28 / E17 / E12 / E13 / E16 / E39 / C
八个分组，加上通用验收（表清单、跨服务 import、CONTRACT_GAPS、事件 schema、
未配置依赖的 503 桩、重启恢复、HTTP 层）。

三条主线：

1. **"没查成"永远不是 pass。** 依赖 503 / 连接断会把整条命令拒掉
   （`DEPENDENCY_UNAVAILABLE`），而不是留下一个"门看起来通过"的包
   （E37-S43-04）。未配置的上游是必然 503 的桩，不是返回值。
2. **外发不是送达。** `PENDING`（人工交接）与 `UNKNOWN`（连接断）都不是
   成功；只有带签名回执或对账确认才是 `SUCCEEDED`。
3. **两个坐标分两列。** `execution_fence`（run/branch）与 `node_epoch`
   （单个 logical_node）从不互相赋值、不互相比较；S43 也不持有任何接受
   指针——G1 比较的永远是 fence 与 fence。

上游全部是闭包 stub：**不联网、不真上传、不真分发、不调付费接口**。
注入的 `external_publish_enabled=True` 只在这份测试的 stub 连接器里成立；
API 层默认 False，`test_http_publish_refuses_when_switches_are_off` 与
`test_service_defaults_to_no_external_publish` 证明这一点。

每个用例的 docstring 第一行是它在方案里的编号。方案 §9 **没有 `T43-*`
编号**，用的是 E/C 编号；`test_plan_ids_are_covered_both_ways` 用 AST 与
方案原文双向比对。
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
from collections import Counter
from pathlib import Path

import pytest
from idempotency_sdk import (
    CommandConflict,
    CommandInProgress,
    SqliteIdempotencyStore,
)

from delivery import deps
from delivery.schema import (
    CANONICAL_VERSION,
    CONTRACT_GAPS,
    EVENTS,
    FIXED_ROLES,
    GATE_IDS,
    ITEM_ROLES,
    SOURCE,
)
from delivery.service import (
    BLOCKING_GATES,
    DeliveryService,
    ServiceError,
    build_manifest,
    manifest_hash_for,
    stable_request_hash,
)

ROOT = Path(__file__).resolve().parents[3]
CONTRACTS = ROOT / "contracts"
SERVICES = ROOT / "services"
PKG_DIR = Path(__file__).resolve().parents[1] / "src" / "delivery"
APP_PATH = Path(__file__).resolve().parents[1] / "api" / "app.py"
PLAN_PATH = ROOT / "plans" / "S43-delivery.md"

TENANT = "t1"
OTHER = "t2"
H = "a" * 64
DH = "b" * 64

CASE_ID = re.compile(r"(?:E\d+-S43-\d{2}|C-S43-\d{2})")

# 16 张自有表 = 14 张 DDL 表 + outbox + inbox。
# `idempotency_record` 由平台 idempotency SDK 建，不算本服务自有。
OWNED_TABLES = {
    "audit_intent",
    "candidate_projection",
    "channel",
    "distribution_attempt",
    "inbox",
    "integrity_signature",
    "item",
    "manifest",
    "manifest_projection",
    "outbox",
    "package",
    "publish_authorization_ref",
    "receipt",
    "receipt_callback",
    "subtitle_projection",
    "voice_authorization_projection",
}


# --------------------------------------------------------------------- 输入


def body(**kw) -> dict:
    """一份能通过全部门的 formal 包请求。"""
    payload = {
        "command_id": "c1",
        "context": {
            "project_id": "p1",
            "series_id": "s1",
            "episode_id": "e1",
            "branch_id": "b1",
            "execution_fence": 3,
            "node_epoch": 7,
            "home_cell": "c",
            "manifest_ref": {"id": "man1"},
            "actor": {"principal_id": "u1", "principal_type": "human"},
        },
        "package_type": "formal",
        "purpose": "broadcast",
        "master_ref": {"id": "m1", "node_id": "n1", "content_hash": H},
        "master_candidate_id": "mc1",
        "timeline_ref": {"id": "tl1", "revision": 4},
        "requested_locales": ["zh-CN"],
        "items": [
            {"role": "master_video", "artifact_ref": {"id": "av1"},
             "content_hash": H, "byte_size": 100, "mime": "mp4_h264_aac"},
            {"role": "qc_evidence",
             "external_ref": {"service_id": "S37", "report_id": "r1"}},
            {"role": "policy_snapshot",
             "external_ref": {"service_id": "S02", "decision_id": "pd1"}},
            {"role": "subtitle", "artifact_ref": {"id": "sub1"},
             "locale": "zh-CN", "content_hash": H, "byte_size": 10,
             "mime": "text/vtt"},
        ],
    }
    payload.update(kw)
    return payload


CHAN = {
    "kind": "partner_api",
    "endpoint_ref": {"secret_ref": "x"},
    "accepted_formats": ["mp4_h264_aac", "text/vtt"],
    "required_artifacts": ["master_video"],
    "max_file_bytes": 1000,
    "supports_query": True,
    "enabled": True,
}

PB = {
    "channel_id": "ch1",
    "command_id": "pub1",
    "publish_authorization_ref": {"policy_decision_id": "pd1",
                                  "decision_hash": DH},
}


# ------------------------------------------------------------------- 测试台


class Harness:
    """内存库 + 全闭包 stub。构造顺序与 `api/app.py` 一致。

    `SqliteIdempotencyStore` **必须在 service 之后构造**：service 的
    `create()` 会 `UPDATE idempotency_record`，表还不存在时那是
    `no such table`。
    """

    SOURCE_STUBS = {
        "accepted_master_source": "st_accepted_master",
        "artifact_source": "st_artifact",
        "timeline_source": "st_timeline",
        "candidate_source": "st_candidate",
        "qc_source": "st_qc",
        "subtitle_source": "st_subtitle",
        "rights_source": "st_rights",
        "policy_source": "st_policy",
        "memory_source": "st_memory",
        "binding_source": "st_binding",
        "cost_source": "st_cost",
        "audit_sink": "st_audit",
        "signer": "st_sign",
        "connector": "st_connector",
        "receipt_verifier": "st_verify",
    }

    def __init__(self, conn=None, *, enabled=True, ttl_hours=24.0,
                 signer_key_ref=None, **over):
        # `check_same_thread=False` 与 `api/app.py` 一致：FastAPI 把同步路由
        # 丢到线程池，连接必须允许跨线程使用。
        self.conn = (conn if conn is not None
                     else sqlite3.connect(":memory:", check_same_thread=False,
                                          isolation_level=None))
        self.conn.row_factory = sqlite3.Row
        self.calls = Counter()
        stubs = {}
        for name, attr in self.SOURCE_STUBS.items():
            # 覆盖也要计数：否则 `harness.calls[...]` 对覆盖掉的依赖永远是 0，
            # "连接器没被再调一次"这类断言会假绿。
            stubs[name] = self._counted(name, over.pop(name)
                                        if name in over else getattr(self, attr))
        if over:
            raise TypeError(f"unknown source overrides: {sorted(over)}")
        self.service = DeliveryService(
            self.conn,
            external_publish_enabled=enabled,
            unknown_ttl_hours=ttl_hours,
            signer_key_ref=signer_key_ref,
            **stubs,
        )
        self._idem = SqliteIdempotencyStore(self.conn)

    # -- 默认上游 -----------------------------------------------------
    def st_accepted_master(self, tenant, manifest_id):
        return {"manifest": {"id": manifest_id, "execution_fence": 3},
                "pointers": {"nodes": [{"node_id": "n1",
                                        "accepted_artifact_ref": "m1"}]}}

    def st_artifact(self, tenant, ref):
        return {"id": ref.get("id"), "state": "ready",
                "content_hash": H, "size_bytes": 100}

    def st_timeline(self, tenant, ref):
        return {"eligible": True, "formal_master_eligible": True}

    def st_candidate(self, tenant, ref):
        return {"deliverable": True, "stale_inputs": [],
                "loudness_gate": "PASSED", "master_spec_ok": True}

    def st_qc(self, tenant, ref):
        return {"decision": "pass", "blocking_count": 0}

    def st_subtitle(self, tenant, query):
        return {"tracks": [{"artifact_ref": {"id": "sub1"},
                            "timeline_revision": 4}]}

    def st_rights(self, tenant, query):
        return {"allowed": True}

    def st_policy(self, tenant, query):
        return {"verdict": "allow", "decision_hash": DH,
                "valid_until": "2099-01-01T00:00:00Z"}

    def st_memory(self, tenant, query):
        return {"checkpoint": "cp", "checkpoint_hash": "c" * 64}

    def st_binding(self, tenant, query):
        return {"binding_set_id": "s", "revision": 1}

    def st_cost(self, tenant, episode):
        return {"as_of": "x", "currencies": {"CNY": 1}}

    def st_audit(self, tenant, intent):
        return {"record_id": "ar1"}

    def st_sign(self, tenant, manifest_hash, key_ref):
        return {"verified": True, "algorithm": "ed25519",
                "signature_b64": "sig"}

    def st_connector(self, tenant, request):
        return {"outcome": "succeeded", "remote_id": "rem1",
                "bytes_sent": 110, "receipt": {"ok": True}}

    def st_verify(self, tenant, package_id, attempt_id, receipt):
        return True

    # -- 工具 ---------------------------------------------------------
    def _counted(self, name, fn):
        def _inner(*args, **kwargs):
            self.calls[name] += 1
            return fn(*args, **kwargs)
        return _inner

    def command(self, tenant, payload, execute):
        """与 `api/app.py` 的 `command()` 等价的幂等外壳。"""
        request_hash = stable_request_hash(payload)
        record = self._idem.begin(tenant, payload["command_id"], request_hash)
        if record.result is not None:
            replay = json.loads(record.result)
            replay["replayed"] = True
            return replay
        try:
            result = execute(request_hash)
        except Exception:
            try:
                self._idem.abort(tenant, payload["command_id"])
            except sqlite3.OperationalError:
                pass
            raise
        self._idem.finish(tenant, payload["command_id"],
                          json.dumps(result, ensure_ascii=False))
        return {**result, "replayed": False}

    def create(self, payload=None, tenant=TENANT):
        payload = body() if payload is None else payload
        return self.command(
            tenant, payload,
            lambda h: self.service.create(tenant, payload["command_id"],
                                          payload, h))

    def seal(self, package_id, command_id="s1", payload=None, tenant=TENANT):
        payload = ({"command_id": command_id} if payload is None else payload)
        return self.command(
            tenant, payload,
            lambda h: self.service.seal(tenant, command_id, package_id, h))

    def publish(self, package_id, payload=None, tenant=TENANT):
        payload = dict(PB) if payload is None else payload
        return self.command(
            tenant, payload,
            lambda h: self.service.publish(tenant, payload["command_id"],
                                           package_id, payload, h))

    def reconcile(self, package_id, payload=None, tenant=TENANT):
        payload = {"command_id": "rec1"} if payload is None else payload
        return self.command(
            tenant, payload,
            lambda h: self.service.reconcile(tenant, payload["command_id"],
                                             package_id, payload, h))

    def receipt(self, payload, tenant=TENANT):
        return self.command(
            tenant, payload,
            lambda h: self.service.ingest_receipt(
                tenant, payload["command_id"], payload, h))

    def events(self):
        rows = self.conn.execute(
            "SELECT * FROM outbox ORDER BY sequence").fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["envelope"] = json.loads(item["payload"])
            out.append(item)
        return out

    def event_types(self):
        return [event["event_type"] for event in self.events()]

    def event_payload(self, event_type):
        found = [event["envelope"]["data"] for event in self.events()
                 if event["event_type"] == event_type]
        return found

    def count(self, table):
        return self.conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def tables(self):
        return {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

    def audits(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT * FROM audit_intent ORDER BY sent_at, intent_id")]

    def packaged(self, package_id, tenant=TENANT):
        return dict(self.conn.execute(
            "SELECT * FROM package WHERE tenant_id=? AND package_id=?",
            (tenant, package_id)).fetchone())


@pytest.fixture
def h():
    return Harness()


# ---------------------------------------------------------------- E37 完整性


def test_E37_S43_01_missing_material_package_reports_gaps(h):
    """E37-S43-01 缺 3 个素材 → missing_material 包创建成功、gaps 明细、
    deliverable=false。"""
    def gone(_tenant, ref):
        raise ServiceError(404, "NOT_FOUND", "gone")

    h.service.artifact_source = gone
    payload = body(
        package_type="missing_material", master_ref=None,
        master_candidate_id=None, timeline_ref=None,
        items=[
            {"role": "stem_dialogue", "artifact_ref": {"id": "x1"}},
            {"role": "stem_sfx", "artifact_ref": {"id": "x2"}},
            {"role": "stem_bgm", "artifact_ref": {"id": "x3"}},
        ])
    result = h.create(payload)

    assert result["package_type"] == "missing_material"
    assert result["state"] == "SEALED"        # 缺素材清单本身是正常产品
    assert result["deliverable"] is False     # 但绝不能当正式包
    assert result["retryable"] is False
    assert result["gate_results"]["G2"] == "pass"
    kinds = [(gap["role"], gap["code"], gap["gate"]) for gap in result["gaps"]]
    assert kinds == [
        ("stem_dialogue", "ARTIFACT_MISSING", "G2"),
        ("stem_sfx", "ARTIFACT_MISSING", "G2"),
        ("stem_bgm", "ARTIFACT_MISSING", "G2"),
    ]
    # 清单确实被冻结成 manifest，客户拿到的是"还差什么"
    manifest = h.service.get_manifest(TENANT, result["package_id"])
    roles = {entry["role"] for entry in manifest["manifest"]["items"]}
    assert {"stem_dialogue", "stem_sfx", "stem_bgm"} <= roles


def test_E37_S43_02_same_material_as_formal_stays_blocked(h):
    """E37-S43-02 同素材建 formal → BLOCKED，不进 SEALED。"""
    def gone(_tenant, ref):
        raise ServiceError(404, "NOT_FOUND", "gone")

    h.service.artifact_source = gone
    payload = body(package_type="formal", items=[
        {"role": "stem_dialogue", "artifact_ref": {"id": "x1"}},
        {"role": "stem_sfx", "artifact_ref": {"id": "x2"}},
        {"role": "stem_bgm", "artifact_ref": {"id": "x3"}},
    ])
    result = h.create(payload)

    assert result["state"] == "BLOCKED"
    assert result["deliverable"] is False
    assert result["gate_results"]["G2"] == "fail"
    assert result["gaps"], "BLOCKED 必须能说出缺什么"
    # 没有 manifest，也没有签名：封包从未发生
    assert h.count("manifest") == 0
    assert h.count("integrity_signature") == 0
    with pytest.raises(ServiceError) as caught:
        h.seal(result["package_id"])
    assert (caught.value.status, caught.value.code) == (
        409, "PACKAGE_BLOCKED")
    assert caught.value.details["gaps"], "409 要带回 gaps 明细"


def _deny_master(_tenant, manifest_id):
    return {"manifest": {"id": manifest_id, "execution_fence": 3},
            "pointers": {"nodes": [{"node_id": "n1",
                                    "accepted_artifact_ref": "m1x"}]}}


def _missing_artifact(_tenant, ref):
    raise ServiceError(404, "NOT_FOUND", "gone")


def _stale_timeline(_tenant, ref):
    return {"eligible": False, "formal_master_eligible": False,
            "state": "incomplete"}


def _blocking_qc(_tenant, ref):
    return {"decision": "repair", "blocking_count": 2}


def _loudness_failed(_tenant, ref):
    return {"deliverable": True, "stale_inputs": [],
            "loudness_gate": "FAILED", "master_spec_ok": True}


def _no_subtitles(_tenant, query):
    return {"tracks": []}


def _rights_denied(_tenant, query):
    return {"allowed": False, "denied": ["v1"], "reasons": ["contract ended"]}


def _memory_down(_tenant, query):
    raise ServiceError(503, "MEMORY_SOURCE_UNAVAILABLE", "S16 down")


# 十道门各自独立挡住。G1–G7、G10 在创建时挡；G8 / G9 在创建时是
# `deferred`（策略门与签名门都还没有可判的东西），在**使用的当口**挡。
GATE_CASES = [
    ("G1", {"accepted_master_source": _deny_master}, "MASTER_NOT_ACCEPTED"),
    ("G2", {"artifact_source": _missing_artifact}, "ARTIFACT_MISSING"),
    ("G3", {"timeline_source": _stale_timeline}, "INCOMPLETE_TIMELINE"),
    ("G4", {"qc_source": _blocking_qc}, "QC_BLOCKING_ISSUE"),
    ("G5", {"candidate_source": _loudness_failed}, "LOUDNESS_GATE_FAILED"),
    ("G6", {"subtitle_source": _no_subtitles}, "SUBTITLE_MISSING"),
    ("G7", {"rights_source": _rights_denied}, "RIGHTS_REVOKED"),
    ("G10", {"memory_source": _memory_down}, "MEMORY_CHECKPOINT_MISSING"),
]


@pytest.mark.parametrize("gate,over,code", GATE_CASES,
                         ids=[case[0] for case in GATE_CASES])
def test_E37_S43_03_each_gate_blocks_on_its_own(gate, over, code):
    """E37-S43-03 十道门逐项注入失败：每道门都能独立挡住（G1–G7、G10）。"""
    harness = Harness(**over)
    payload = body()
    if gate == "G7":
        payload = body(voice_refs=[{"voice_id": "v1"}])
    result = harness.create(payload)

    assert result["state"] == "BLOCKED"
    assert result["gate_results"][gate] == "fail"
    assert (gate, code) in {(gap["gate"], gap["code"])
                            for gap in result["gaps"]}
    # 只有被注入的那道门失败：别的门没有被顺带判成 fail（门是独立的）
    others = {gid: value for gid, value in result["gate_results"].items()
              if gid != gate}
    assert "fail" not in others.values(), others


def test_E37_S43_03_g8_policy_gate_blocks_at_publish(h):
    """E37-S43-03（G8）策略门创建时 `deferred`，外发当口才判定。"""
    result = h.create()
    assert result["state"] == "SEALED"
    assert result["gate_results"]["G8"] == "deferred"

    h.service.policy_source = lambda tenant, query: (
        {"verdict": "deny", "reasons": ["episode not cleared"]}
        if query.get("op") == "evaluate" else
        {"verdict": "allow", "decision_hash": DH,
         "valid_until": "2099-01-01T00:00:00Z"})
    h.service.put_channel(TENANT, "ch1", CHAN)
    with pytest.raises(ServiceError) as caught:
        h.publish(result["package_id"])
    assert (caught.value.status, caught.value.code) == (403, "POLICY_DENIED")
    assert caught.value.details["reasons"] == ["episode not cleared"]
    # 没有 attempt 落库：被拒绝的外发不留痕
    assert h.count("distribution_attempt") == 0


def test_E37_S43_03_g9_signature_gate_blocks_formal_publish(h):
    """E37-S43-03（G9）签名门：创建时 `deferred`，验不过就挡住外发。"""
    harness = Harness(
        signer=lambda tenant, digest, key_ref: {
            "verified": False, "algorithm": "ed25519", "error": "bad key"})
    result = harness.create()

    assert result["state"] == "SEALED"          # 封包本身完成了
    assert result["gate_results"]["G9"] == "fail"
    assert result["deliverable"] is False       # 但不可交付
    harness.service.put_channel(TENANT, "ch1", CHAN)
    with pytest.raises(ServiceError) as caught:
        harness.publish(result["package_id"])
    assert (caught.value.status, caught.value.code) == (
        409, "SIGNATURE_REQUIRED")
    assert caught.value.details["reason"] == "bad key"
    assert harness.calls["connector"] == 0


def test_E37_S43_04_dependency_503_is_not_a_pass(h):
    """E37-S43-04 依赖 503 → 门为"未知"，整体拒绝（不当作 pass）。"""
    def down(_tenant, ref):
        raise ServiceError(503, "ARTIFACT_SOURCE_UNAVAILABLE", "S28 down")

    h.service.artifact_source = down
    with pytest.raises(ServiceError) as caught:
        h.create()
    assert (caught.value.status, caught.value.code) == (
        503, "DEPENDENCY_UNAVAILABLE")
    assert caught.value.retryable is True
    assert caught.value.details == {"gate": "G2", "upstream": "S28",
                                    "upstream_code": "ARTIFACT_SOURCE_UNAVAILABLE"}
    # 什么都没落库：不是"建了一个看不出来的包"
    assert h.count("package") == 0
    assert h.count("outbox") == 0


def test_E37_S43_04_connection_error_is_also_not_a_pass(h):
    """E37-S43-04 连接断与 503 同罪：门不能判定就是整体拒绝。"""
    def unreachable(_tenant, ref):
        raise ConnectionError("connection reset")

    h.service.artifact_source = unreachable
    with pytest.raises(ServiceError) as caught:
        h.create()
    assert (caught.value.status, caught.value.code) == (
        503, "DEPENDENCY_UNAVAILABLE")
    assert caught.value.details["upstream_code"] == "UNREACHABLE"
    assert h.count("package") == 0


def test_E37_S43_04_upstream_4xx_is_explained_not_swallowed(h):
    """E37-S43-04 上游 4xx 不是"依赖不可用"：折成门自己的原因。"""
    def filtered(_tenant, ref):
        return {"id": ref.get("id"), "state": "ready",
                "permission_filtered": True}

    h.service.artifact_source = filtered
    result = h.create()
    assert result["state"] == "BLOCKED"
    assert (result["gaps"][0]["code"], result["gaps"][0]["detail"]["reason"]) \
        == ("ARTIFACT_MISSING", "permission_filtered")


def test_E37_S43_05_preview_survives_loudness_failure_but_is_not_formal(h):
    """E37-S43-05 preview 包在 loudness_gate=FAILED 时仍可创建，但
    deliverable=false 且不可当正式包。"""
    harness = Harness(candidate_source=_loudness_failed)
    result = harness.create(body(package_type="preview"))

    assert result["package_type"] == "preview"
    assert result["state"] == "SEALED"
    assert result["deliverable"] is False
    # G5 不是 preview 的阻断门：如实记 not_applicable，而不是偷偷记 pass
    assert result["gate_results"]["G5"] == "not_applicable"
    assert "G5" not in BLOCKING_GATES["preview"]
    # 同一份请求改成 formal 就被挡住 —— preview 不能冒充正式包
    other = Harness(candidate_source=_loudness_failed)
    formal = other.create(body(package_type="formal"))
    assert formal["state"] == "BLOCKED"
    assert formal["gate_results"]["G5"] == "fail"


def test_E37_S43_06_missing_memory_checkpoint_blocks_manifest(h):
    """E37-S43-06 manifest 缺 memory_checkpoint → MANIFEST_INCOMPLETE。"""
    harness = Harness(memory_source=_memory_down)
    result = harness.create()

    assert result["state"] == "BLOCKED"
    assert result["gate_results"]["G10"] == "fail"
    gap = [item for item in result["gaps"]
           if item["role"] == "memory_checkpoint"]
    assert gap and gap[0]["code"] == "MEMORY_CHECKPOINT_MISSING"
    assert gap[0]["gate"] == "G10"
    assert harness.count("manifest") == 0


def test_E37_S43_07_superseded_master_blocks_publish(h):
    """E37-S43-07 master 在创建后被 supersede → 外发 MASTER_SUPERSEDED。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    result = h.create()
    assert result["state"] == "SEALED"

    h.service.accepted_master_source = _deny_master   # S08 现在指向 m1x
    with pytest.raises(ServiceError) as caught:
        h.publish(result["package_id"])
    assert (caught.value.status, caught.value.code) == (
        409, "MASTER_SUPERSEDED")
    assert caught.value.details["accepted"] == "m1x"
    assert caught.value.details["referenced"] == "m1"
    assert h.count("distribution_attempt") == 0
    assert h.calls["connector"] == 0


def test_E37_S43_08_allow_superseded_needs_a_fresh_allow(h):
    """E37-S43-08 `allow_superseded=true` + S02 allow → 历史版本可补发；
    无授权 → 拒绝。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    result = h.create()
    h.service.accepted_master_source = _deny_master

    allowed = h.publish(result["package_id"],
                        {**PB, "allow_superseded": True})
    assert allowed["state"] == "SUCCEEDED"
    assert allowed["dispatched"] is True
    assert h.service.metrics["delivery_superseded_publish_total"] == 1

    # 没有 S02 的 allow：allow_superseded 不是一张免检票
    denied = Harness()
    denied.service.put_channel(TENANT, "ch1", CHAN)
    package = denied.create()
    denied.service.accepted_master_source = _deny_master
    denied.service.policy_source = lambda tenant, query: (
        {"verdict": "deny", "reasons": ["superseded publish not approved"]}
        if query.get("op") == "evaluate" else
        {"verdict": "allow", "decision_hash": DH,
         "valid_until": "2099-01-01T00:00:00Z"})
    with pytest.raises(ServiceError) as caught:
        denied.publish(package["package_id"],
                       {**PB, "allow_superseded": True})
    assert (caught.value.status, caught.value.code) == (403, "POLICY_DENIED")


# ------------------------------------------------------------- E27 撤权越权


def test_E27_S43_01_policy_deny_returns_reasons(h):
    """E27-S43-01 外发时 S02 `deny` → 403，附 `reasons[]`。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    result = h.create()

    def denied(_tenant, query):
        if query.get("op") == "evaluate":
            return {"verdict": "deny", "reasons": ["kill switch", "budget"]}
        return {"verdict": "allow", "decision_hash": DH,
                "valid_until": "2099-01-01T00:00:00Z"}

    h.service.policy_source = denied
    with pytest.raises(ServiceError) as caught:
        h.publish(result["package_id"])
    assert caught.value.status == 403
    assert caught.value.code == "POLICY_DENIED"
    assert caught.value.details["reasons"] == ["kill switch", "budget"]
    assert h.calls["connector"] == 0


def test_E27_S43_02_forged_decision_id_is_rejected(h):
    """E27-S43-02 调用方伪造 `publish_authorization_ref`（decision_id 不
    存在）→ 403。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    result = h.create()

    def lookup_missing(_tenant, query):
        if query.get("op") == "lookup":
            raise ServiceError(404, "DECISION_NOT_FOUND", "no such decision")
        return {"verdict": "allow", "decision_hash": DH,
                "valid_until": "2099-01-01T00:00:00Z"}

    h.service.policy_source = lookup_missing
    with pytest.raises(ServiceError) as caught:
        h.publish(result["package_id"],
                  {**PB, "publish_authorization_ref": {
                      "policy_decision_id": "forged", "decision_hash": DH}})
    assert (caught.value.status, caught.value.code) == (403, "POLICY_DENIED")
    assert caught.value.details == {"policy_decision_id": "forged",
                                    "reason": "decision_not_found"}
    assert h.calls["connector"] == 0


def test_E27_S43_02_mismatched_decision_hash_is_forgery(h):
    """E27-S43-02 引用 id 对但 hash 不对：调用方的票是旧的或伪造的。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    result = h.create()
    with pytest.raises(ServiceError) as caught:
        h.publish(result["package_id"],
                  {**PB, "publish_authorization_ref": {
                      "policy_decision_id": "pd1", "decision_hash": "d" * 64}})
    assert (caught.value.status, caught.value.code) == (403, "POLICY_DENIED")
    assert "hash does not match" in str(caught.value)


def test_E27_S43_03_expired_decision_is_rejected(h):
    """E27-S43-03 判定已过 `valid_until` → 403。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    result = h.create()
    h.service.policy_source = lambda tenant, query: {
        "verdict": "allow", "decision_hash": DH,
        "valid_until": "2000-01-01T00:00:00Z"}
    with pytest.raises(ServiceError) as caught:
        h.publish(result["package_id"])
    assert (caught.value.status, caught.value.code) == (403, "POLICY_DENIED")
    assert caught.value.details["valid_until"] == "2000-01-01T00:00:00Z"
    assert h.calls["connector"] == 0


def test_E27_S43_04_kill_switch_is_live_and_not_cached(h):
    """E27-S43-04 外发前 kill switch 打开 → 拒绝，且不缓存"上次允许"。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    first = h.create()
    # 第一次：允许并通过
    assert h.publish(first["package_id"],
                     {**PB, "command_id": "pub-a"})["state"] == "SUCCEEDED"
    assert h.calls["connector"] == 1

    # 第二个包，创建时 S02 还允许；外发时 kill switch 打开
    second = h.create(body(command_id="c2"),
                      tenant=TENANT)
    h.service.policy_source = lambda tenant, query: (
        {"verdict": "allow", "kill_switch": True,
         "reasons": ["operator stopped external publish"]}
        if query.get("op") == "evaluate" else
        {"verdict": "allow", "decision_hash": DH,
         "valid_until": "2099-01-01T00:00:00Z"})
    with pytest.raises(ServiceError) as caught:
        h.publish(second["package_id"], {**PB, "command_id": "pub-b"})
    assert (caught.value.status, caught.value.code) == (403, "POLICY_DENIED")
    assert "kill switch" in str(caught.value)
    assert caught.value.details["reasons"] == [
        "operator stopped external publish"]
    # 第一次的成功没有被复用成第二次的放行
    assert h.calls["connector"] == 1


def test_E27_S43_05_require_human_without_decision_is_rejected(h):
    """E27-S43-05 `require_human` 无 S40 决定 → 403。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    result = h.create()
    h.service.policy_source = lambda tenant, query: (
        {"verdict": "require_human", "reasons": ["manual review needed"]}
        if query.get("op") == "evaluate" else
        {"verdict": "allow", "decision_hash": DH,
         "valid_until": "2099-01-01T00:00:00Z"})
    with pytest.raises(ServiceError) as caught:
        h.publish(result["package_id"])
    assert (caught.value.status, caught.value.code) == (403, "POLICY_DENIED")
    assert caught.value.details["verdict"] == "require_human"
    assert h.calls["connector"] == 0


def test_E27_S43_05_require_human_with_s40_reference_passes(h):
    """E27-S43-05（正例）带 S40 决定引用的 `require_human` 可以放行。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    result = h.create()
    h.service.policy_source = lambda tenant, query: (
        {"verdict": "require_human"}
        if query.get("op") == "evaluate" else
        {"verdict": "allow", "decision_hash": DH,
         "valid_until": "2099-01-01T00:00:00Z"})
    out = h.publish(result["package_id"],
                    {**PB, "human_decision_ref": {"decision_id": "s40-1"}})
    assert out["state"] == "SUCCEEDED"
    ref = dict(h.conn.execute(
        "SELECT * FROM publish_authorization_ref WHERE tenant_id=?",
        (TENANT,)).fetchone())
    assert ref["human_decision_ref"] is not None


# --------------------------------------------------------------- E28 跨租户


def test_E28_S43_01_cross_tenant_read_is_identical_404(h):
    """E28-S43-01 跨租户读包 → 404（与不存在完全一样，不泄漏存在性）。"""
    result = h.create()
    package_id = result["package_id"]

    with pytest.raises(ServiceError) as foreign:
        h.service.get_package(OTHER, package_id)
    with pytest.raises(ServiceError) as missing:
        h.service.get_package(OTHER, "dp_does_not_exist")

    assert foreign.value.status == missing.value.status == 404
    assert foreign.value.code == missing.value.code == "PACKAGE_NOT_FOUND"
    # 消息形状也一样，只有 id 不同 —— 不泄漏"这个 id 存在，只是不属于你"
    assert str(foreign.value).replace(package_id, "<id>") == \
        str(missing.value).replace("dp_does_not_exist", "<id>")
    # 其它入口同样不泄漏
    for call in (lambda: h.service.get_manifest(OTHER, package_id),
                 lambda: h.service.get_evidence(OTHER, package_id),
                 lambda: h.service.get_attempts(OTHER, package_id)):
        with pytest.raises(ServiceError) as caught:
            call()
        assert (caught.value.status, caught.value.code) == (
            404, "PACKAGE_NOT_FOUND")
    assert h.service.list_packages(OTHER)["items"] == []


def test_E28_S43_02_artifact_permission_filter_is_not_existence_leak(h):
    """E28-S43-02 跨租户引用 master/artifact → S08/S28 拒绝；S43 不泄漏
    存在性（上游按权限过滤时按"缺失"处理，不猜"存在但你没权限"）。"""
    def filtered(_tenant, ref):
        return {"id": ref.get("id"), "state": "ready",
                "permission_filtered": True}

    h.service.artifact_source = filtered
    result = h.create()
    assert result["state"] == "BLOCKED"
    gap = result["gaps"][0]
    assert gap["code"] == "ARTIFACT_MISSING"
    assert gap["detail"] == {"artifact_id": "av1",
                             "reason": "permission_filtered"}


def test_E28_S43_03_signing_key_is_tenant_scoped(h):
    """E28-S43-03 签名 key 按租户隔离，A 租户的 key 不能验 B 的包。"""
    seen = {}

    def capture(tenant, digest, key_ref):
        seen[tenant] = dict(key_ref)
        return {"verified": True, "algorithm": "ed25519",
                "signature_b64": f"sig-{tenant}"}

    harness = Harness(signer=capture)
    harness.create(body())
    harness.create(body(command_id="c2",
                        context={**body()["context"],
                                 "project_id": "p2"}),
                  tenant=OTHER)

    assert seen[TENANT]["tenant_id"] == TENANT
    assert seen[OTHER]["tenant_id"] == OTHER
    assert seen[TENANT] != seen[OTHER]
    # 库里存的 key_ref 也带租户，事后可追溯"用哪把钥匙签的"
    rows = [dict(row) for row in harness.conn.execute(
        "SELECT tenant_id, key_ref FROM integrity_signature")]
    for row in rows:
        assert json.loads(row["key_ref"])["tenant_id"] == row["tenant_id"]
    # 验 B 的包不能拿到 A 的签名
    b_package = harness.conn.execute(
        "SELECT package_id FROM package WHERE tenant_id=?",
        (OTHER,)).fetchone()[0]
    with pytest.raises(ServiceError):
        harness.service.get_manifest(TENANT, b_package)


def test_E28_S43_04_channels_are_tenant_isolated(h):
    """E28-S43-04 渠道配置按租户隔离。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    assert [c["channel_id"] for c in h.service.list_channels(TENANT)["items"]] \
        == ["ch1"]
    assert h.service.list_channels(OTHER)["items"] == []
    # B 租户看不见、也用不了 A 的渠道
    with pytest.raises(ServiceError) as caught:
        h.service.publish(OTHER, "pub1", h.create()["package_id"],
                          PB, "h")
    assert (caught.value.status, caught.value.code) == (
        404, "PACKAGE_NOT_FOUND")

    other_package = h.create(body(command_id="c2"), tenant=OTHER)
    with pytest.raises(ServiceError) as caught:
        h.service.publish(OTHER, "pub2", other_package["package_id"],
                          PB, "h")
    assert (caught.value.status, caught.value.code) == (
        404, "CHANNEL_NOT_FOUND")
    # A 的渠道配置完好，没被 B 的操作改动
    assert h.service.list_channels(TENANT)["items"][0]["channel_id"] == "ch1"


# ----------------------------------------------------- E17 回执乱序/重复/伪造


def _manual_harness(**over):
    """人工交接渠道：`publish` 停在 IN_FLIGHT，等回执。"""
    harness = Harness(**over)
    harness.service.put_channel(TENANT, "ch1", {**CHAN, "kind": "manual_handoff"})
    return harness


def test_E17_S43_01_receipt_before_attempt_is_staged_then_applied(h):
    """E17-S43-01 渠道回调先于 attempt 落库 → 暂存，attempt 出现后应用；
    **已经收过的包不再外发一次**。"""
    harness = _manual_harness()
    package = harness.create()
    package_id = package["package_id"]

    staged = harness.receipt({
        "command_id": "rc1", "package_id": package_id,
        "channel_id": "ch1", "remote_id": "rem9", "receipt_id": "rr1"})
    assert staged["accepted"] is True
    assert staged["staged"] is True
    assert staged["callback_id"].startswith("cb_")
    # 暂存不是送达：没有 receipt、包没变成 PUBLISHED
    assert harness.count("receipt") == 0
    assert harness.packaged(package_id)["state"] == "SEALED"
    assert harness.count("receipt_callback") == 1

    attempt = harness.publish(package_id)
    # attempt 一落库，暂存的回执就被应用掉：渠道已经收下了。
    assert attempt["state"] == "SUCCEEDED"
    assert attempt["package_state"] == "PUBLISHED"
    assert attempt["dispatched"] is False
    assert "nothing was sent again" in attempt["note"]
    # 有已验证的回执就不能再调连接器 —— 否则客户会收到第二份
    assert harness.calls["connector"] == 0

    row = dict(harness.conn.execute(
        "SELECT * FROM receipt_callback").fetchone())
    assert row["applied_at"] is not None
    receipts = [dict(r) for r in harness.conn.execute(
        "SELECT * FROM receipt")]
    assert len(receipts) == 1
    assert receipts[0]["remote_id"] == "rem9"
    assert receipts[0]["attempt_id"] == attempt["attempt_id"]
    assert harness.event_types().count("delivery.package.published") == 1

    # 已经 SUCCEEDED：对账是无操作，也不会把结果往回拨
    confirmed = harness.reconcile(package_id, {
        "command_id": "rec1", "attempt_id": attempt["attempt_id"]})
    assert confirmed["state"] == "SUCCEEDED"
    assert confirmed["package_state"] == "PUBLISHED"
    assert "no-op" in confirmed["note"]
    assert harness.calls["connector"] == 0


def test_E17_S43_02_duplicate_receipt_makes_no_second_receipt(h):
    """E17-S43-02 重复回执 → 幂等，不产生第二个 receipt。"""
    harness = Harness()
    harness.service.put_channel(TENANT, "ch1", CHAN)
    package = harness.create()
    attempt = harness.publish(package["package_id"])
    assert attempt["state"] == "SUCCEEDED"

    payload = {"command_id": "rc1", "package_id": package["package_id"],
               "channel_id": "ch1", "attempt_id": attempt["attempt_id"],
               "remote_id": "remX", "receipt_id": "rrX"}
    first = harness.receipt(payload)
    second = harness.receipt({**payload, "command_id": "rc2"})

    assert first["receipt_id"] == second["receipt_id"]
    assert first["attempt_state"] == second["attempt_state"] == "SUCCEEDED"
    assert harness.count("receipt") == 1
    # 第二条不再推进：如实说明"终态没变"，而不是再发一个 published 事件
    assert "already terminal" in second["note"]
    assert harness.event_types().count("delivery.package.published") == 1


def test_E17_S43_03_forged_receipt_signature_is_refused_and_audited(h):
    """E17-S43-03 伪签名回执 → 拒绝，记审计。"""
    harness = Harness(receipt_verifier=lambda *a: False)
    harness.service.put_channel(TENANT, "ch1", CHAN)
    package = harness.create()
    attempt = harness.publish(package["package_id"])
    # 这次 publish 自己带回执，所以基线不是 0
    before = harness.count("receipt")

    with pytest.raises(ServiceError) as caught:
        harness.receipt({
            "command_id": "rc1", "package_id": package["package_id"],
            "channel_id": "ch1", "attempt_id": attempt["attempt_id"],
            "remote_id": "remX"})
    assert (caught.value.status, caught.value.code) == (
        403, "RECEIPT_SIGNATURE_INVALID")
    assert harness.count("receipt") == before
    audit = [row for row in harness.audits()
             if row["action"] == "delivery.receipt.rejected"]
    assert audit, "拒收回执必须留审计"
    assert audit[0]["outcome"] == "DENIED"
    assert audit[0]["delivered"] == 1
    assert harness.service.metrics["delivery_receipt_rejected_total"] == 1


def test_E17_S43_03_unverifiable_receipt_is_503_not_acceptance(h):
    """E17-S43-03 验签服务不可达：503 + 审计，绝不"先收下再说"。"""
    def down(*_args):
        raise ServiceError(503, "RECEIPT_VERIFIER_UNAVAILABLE", "verifier down")

    harness = Harness(receipt_verifier=down)
    with pytest.raises(ServiceError) as caught:
        harness.receipt({"command_id": "rc1", "package_id": "dp_x",
                         "channel_id": "ch1", "remote_id": "r"})
    assert (caught.value.status, caught.value.code) == (
        503, "DEPENDENCY_UNAVAILABLE")
    assert harness.count("receipt_callback") == 0
    rejected = [row for row in harness.audits()
                if row["action"] == "delivery.receipt.rejected"]
    assert rejected and rejected[0]["outcome"] == "ERROR"


def test_E17_S43_04_terminal_attempt_never_goes_backwards(h):
    """E17-S43-04 回执乱序（旧状态覆盖新状态）→ 终态不倒退。"""
    harness = Harness()
    harness.service.put_channel(TENANT, "ch1", CHAN)
    package = harness.create()
    attempt = harness.publish(package["package_id"])
    assert attempt["state"] == "SUCCEEDED"

    # 一个迟到的"处理中"回执：不能把 SUCCEEDED 拉回 IN_FLIGHT/PENDING
    late = harness.receipt({
        "command_id": "rc1", "package_id": package["package_id"],
        "channel_id": "ch1", "attempt_id": attempt["attempt_id"],
        "remote_id": "remX", "state": "IN_FLIGHT"})
    assert late["attempt_state"] == "SUCCEEDED"
    assert harness.packaged(package["package_id"])["state"] == "PUBLISHED"
    # 对已完成的对账也不再改结果
    noop = harness.reconcile(package["package_id"],
                            {"command_id": "rec1",
                             "attempt_id": attempt["attempt_id"]})
    assert noop["state"] == "SUCCEEDED"
    assert "no-op" in noop["note"]


def test_E17_S43_04_stale_projection_seq_does_not_overwrite(h):
    """E17-S43-04（投影侧）乱序事件不能覆盖更新的投影。"""
    newer = {"event_id": "ev-new", "type": "asset.voice.authorization.changed",
             "tenant_id": TENANT,
             "data": {"voice_id": "v1", "to_state": "revoked", "state_seq": 9}}
    older = {"event_id": "ev-old", "type": "asset.voice.authorization.changed",
             "tenant_id": TENANT,
             "data": {"voice_id": "v1", "to_state": "active", "state_seq": 3}}
    assert h.service.consume_event(TENANT, newer)["accepted"] is True
    assert h.service.consume_event(TENANT, older)["accepted"] is True

    row = dict(h.conn.execute(
        "SELECT * FROM voice_authorization_projection WHERE tenant_id=?",
        (TENANT,)).fetchone())
    assert (row["state"], row["state_seq"]) == ("revoked", 9)


# ---------------------------------------------------------------- E12 幂等


def test_E12_S43_01_replayed_publish_reuses_the_same_attempt(h):
    """E12-S43-01 同 command 重投 publish → 同一 attempt，不重复外发。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    package = h.create()
    first = h.publish(package["package_id"])
    second = h.publish(package["package_id"])       # 同 command_id = pub1

    assert first["replayed"] is False
    assert second["replayed"] is True
    assert first["attempt_id"] == second["attempt_id"]
    assert h.count("distribution_attempt") == 1
    assert h.calls["connector"] == 1
    assert h.event_types().count("delivery.package.published") == 1


def test_E12_S43_01_replayed_create_reuses_the_package(h):
    """E12-S43-01（create 侧）同 command 重投 → 同 package_id、同 hash。"""
    first = h.create()
    second = h.create()
    assert second["replayed"] is True
    assert first["package_id"] == second["package_id"]
    assert first["package_hash"] == second["package_hash"]
    assert first["operation_id"] == second["operation_id"]
    assert h.count("package") == 1
    # create 自动封包，所以一次成功的 create 落两条事件（created + sealed）；
    # 重投不能再多发一条。
    assert Counter(h.event_types()) == {
        "delivery.package.created": 1, "delivery.package.sealed": 1}


def test_E12_S43_02_same_command_different_body_is_409(h):
    """E12-S43-02 同 command 不同 body → 409。"""
    h.create()
    changed = body(purpose="archive")
    with pytest.raises(CommandConflict) as caught:
        h.create(changed)
    assert caught.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert caught.value.retryable is False
    assert caught.value.stored_hash != caught.value.incoming_hash
    assert h.count("package") == 1        # 没有覆盖成新 body


def test_E12_S43_02_volatile_fields_do_not_count_as_a_different_body(h):
    """E12-S43-02（边界）trace_id 变了不是"不同 body"。"""
    h.create()
    retraced = body()
    retraced["context"] = {**retraced["context"], "trace_id": "another",
                           "timestamp": "2030-01-01T00:00:00Z"}
    second = h.create(retraced)
    assert second["replayed"] is True
    assert h.count("package") == 1


def test_E12_S43_03_repeat_seal_returns_the_frozen_manifest(h):
    """E12-S43-03 重复 seal → 返回原 manifest，不重算 hash。"""
    package = h.create()
    package_id = package["package_id"]
    frozen = h.service.get_manifest(TENANT, package_id)
    before = frozen["manifest_hash"]
    signature_before = h.count("integrity_signature")

    again = h.seal(package_id)
    assert again["state"] == "SEALED"
    assert again["manifest_hash"] == before
    assert again["signature_verified"] is True
    assert h.count("integrity_signature") == signature_before
    assert h.service.get_manifest(TENANT, package_id)["manifest_hash"] == before
    # 再封一次也没有第二个 sealed 事件
    assert h.event_types().count("delivery.package.sealed") == 1


# ---------------------------------------------------- E13 / E16 崩溃与 UNKNOWN


def test_E13_S43_01_seal_failure_leaves_no_manifest_and_stays_drafting():
    """E13-S43-01 封包事务中崩溃 → 无 manifest、无 signature，包仍
    DRAFTING。"""
    def signing_down(*_args):
        raise ServiceError(503, "SIGNING_UNAVAILABLE", "KMS is down")

    harness = Harness(signer=signing_down)
    package = harness.create()
    assert package["state"] == "DRAFTING"
    assert package["retryable"] is True
    assert package["seal_error"] == "DEPENDENCY_UNAVAILABLE"

    # 签名恢复，但让 manifest 的写入在事务中途炸掉（真实崩溃的等价物）
    harness.service.signer = lambda tenant, digest, key_ref: {
        "verified": True, "algorithm": "ed25519", "signature_b64": "sig"}
    harness.conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON manifest"
        " BEGIN SELECT RAISE(ABORT, 'disk I/O error'); END")

    with pytest.raises(sqlite3.Error):
        harness.service.seal(TENANT, "s1", package["package_id"], "h")

    assert harness.count("manifest") == 0
    assert harness.count("integrity_signature") == 0
    assert harness.packaged(package["package_id"])["state"] == "DRAFTING"
    assert harness.event_types().count("delivery.package.sealed") == 0

    # 回滚之后重试能成功：事务没有留下半截状态
    harness.conn.execute("DROP TRIGGER boom")
    healed = harness.service.seal(TENANT, "s1", package["package_id"], "h")
    assert healed["state"] == "SEALED"
    assert harness.count("manifest") == 1
    assert harness.count("integrity_signature") == 1


def test_E13_S43_02_interrupted_publish_is_unknown_and_not_resent(h):
    """E13-S43-02 外发中断 → `UNKNOWN`，不自动重传。"""
    def broken_pipe(_tenant, request):
        raise ConnectionError("connection reset by peer")

    harness = Harness(connector=broken_pipe)
    harness.service.put_channel(TENANT, "ch1", CHAN)
    package = harness.create()
    attempt = harness.publish(package["package_id"])

    assert attempt["state"] == "UNKNOWN"
    assert attempt["failure_code"] == "NETWORK_UNREACHABLE"
    assert attempt["reconcile_after"]
    assert harness.packaged(package["package_id"])["state"] == "SEALED"
    assert "reconcile before resending" in attempt["note"]
    # 再投一次 publish 也不会发：同一渠道有在飞（live）attempt
    again = harness.publish(package["package_id"],
                            {**PB, "command_id": "pub2"})
    assert again["dispatched"] is False
    assert again["state"] == "UNKNOWN"
    assert harness.calls["connector"] == 1


def test_E16_S43_01_reconcile_confirms_receipt_to_succeeded(h):
    """E16-S43-01 对账确认已收 → `SUCCEEDED`。"""
    def unreachable(_tenant, request):
        raise ConnectionError("timeout")

    harness = Harness(connector=unreachable)
    harness.service.put_channel(TENANT, "ch1", CHAN)
    package = harness.create()
    attempt = harness.publish(package["package_id"])
    assert attempt["state"] == "UNKNOWN"

    harness.service.connector = lambda tenant, request: {
        "received": True, "remote_id": "rem5", "acknowledged_at": None}
    reconciled = harness.reconcile(package["package_id"], {
        "command_id": "rec1", "attempt_id": attempt["attempt_id"]})

    assert reconciled["state"] == "SUCCEEDED"
    assert reconciled["package_state"] == "PUBLISHED"
    assert reconciled["remote_ref"] == "rem5"
    assert "SUCCEEDED" in reconciled["note"]
    assert harness.count("receipt") == 1
    assert harness.event_types().count("delivery.package.published") == 1


def test_E16_S43_02_reconcile_not_received_returns_to_pending(h):
    """E16-S43-02 对账确认未收 → 回 `PENDING` 重发（显式，不自动）。"""
    def unreachable(_tenant, request):
        raise ConnectionError("timeout")

    harness = Harness(connector=unreachable)
    harness.service.put_channel(TENANT, "ch1", CHAN)
    package = harness.create()
    attempt = harness.publish(package["package_id"])

    # 查询走的是换上去的连接器，它不在 Harness 的计数里，自带计数器。
    queries = []

    def not_received(tenant, request):
        queries.append(request)
        return {"received": False}

    harness.service.connector = not_received
    reconciled = harness.reconcile(package["package_id"], {
        "command_id": "rec1", "attempt_id": attempt["attempt_id"]})

    assert reconciled["state"] == "PENDING"
    assert reconciled["failure_code"] == "RECONCILE_NOT_RECEIVED"
    assert reconciled["reconcile_after"] is None
    assert reconciled["package_state"] == "SEALED"
    assert "no automatic resend" in reconciled["note"]
    # 对账只问了一次，没有发东西
    assert len(queries) == 1
    assert harness.calls["connector"] == 1      # 只有最初那一次外发
    assert harness.count("receipt") == 0
    # 明确的重发：新 attempt，新编号
    harness.service.connector = lambda tenant, request: {
        "outcome": "succeeded", "remote_id": "rem6", "bytes_sent": 110}
    resent = harness.publish(package["package_id"],
                             {**PB, "command_id": "pub2"})
    assert resent["state"] == "SUCCEEDED"
    assert resent["attempt_no"] == 2
    assert harness.packaged(package["package_id"])["state"] == "PUBLISHED"


def test_E16_S43_03_reconcile_ttl_expiry_requires_a_human(h):
    """E16-S43-03 对账超 TTL → `FAILED_UNRESOLVED` + 告警，不自动重发。"""
    def unreachable(_tenant, request):
        raise ConnectionError("timeout")

    harness = Harness(connector=unreachable, ttl_hours=0.0)
    harness.service.put_channel(TENANT, "ch1", CHAN)
    package = harness.create()
    attempt = harness.publish(package["package_id"])
    assert attempt["state"] == "UNKNOWN"

    harness.service.connector = lambda tenant, request: {"received": None}
    expired = harness.reconcile(package["package_id"], {
        "command_id": "rec1", "attempt_id": attempt["attempt_id"]})

    assert expired["state"] == "FAILED_UNRESOLVED"
    assert expired["failure_code"] == "RECONCILE_TIMEOUT"
    assert expired["package_state"] == "PUBLISH_FAILED"
    assert "alert raised" in expired["note"]
    assert harness.event_types().count("delivery.publish.failed") == 1
    alerts = [row for row in harness.audits()
              if row["verb"] == "reconcile"]
    assert alerts and alerts[0]["outcome"] == "REPORTED_UNKNOWN"
    assert harness.service.metrics["delivery_reconcile_timeout_total"] == 1
    # 终态之后既不能重发也不能靠对账翻案
    with pytest.raises(ServiceError) as caught:
        harness.publish(package["package_id"],
                        {**PB, "command_id": "pub2"})
    assert (caught.value.status, caught.value.code) == (
        409, "PACKAGE_PUBLISH_FAILED")


# ------------------------------------------- E39 审计意图与证据可追溯


def test_E39_S43_01_every_seal_and_publish_leaves_an_audit_intent(h):
    """E39-S43-01 每次 seal / publish 都有 S45 意图，且与包状态一致。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    package = h.create()               # 建包即封包 => 一次 seal
    h.publish(package["package_id"])

    audits = h.audits()
    assert [(a["verb"], a["action"], a["outcome"]) for a in audits] == [
        ("seal", "delivery.package.sealed", "SUCCEEDED"),
        ("publish", "delivery.package.published", "SUCCEEDED"),
    ]
    # 每条意图都说得出它属于哪个包，且真的送达了 S45
    assert {a["package_id"] for a in audits} == {package["package_id"]}
    assert all(a["delivered"] == 1 for a in audits)
    assert all(a["record_id"] == "ar1" for a in audits)
    assert h.service.pending_audits(TENANT) == {"undelivered_audit_intents": 0}
    # 包状态与最后一条意图一致：PUBLISHED 只能由 published 意图收尾
    assert h.packaged(package["package_id"])["state"] == "PUBLISHED"


def test_E39_S43_01_failed_publish_audits_the_failure_not_the_success(h):
    """E39-S43-01 终态失败 → 审计记 ERROR，不是"发过了"。"""
    def denied(_tenant, request):
        raise ServiceError(403, "CHANNEL_FORBIDDEN", "no")

    harness = Harness(connector=denied)
    harness.service.put_channel(TENANT, "ch1", CHAN)
    package = harness.create()
    attempt = harness.publish(package["package_id"])
    assert attempt["state"] == "FAILED"

    publishes = [a for a in harness.audits() if a["verb"] == "publish"]
    assert len(publishes) == 1
    assert publishes[0]["action"] == "delivery.publish.failed"
    assert publishes[0]["outcome"] == "ERROR"
    assert harness.packaged(package["package_id"])["state"] == "PUBLISH_FAILED"
    # 失败也照发 sealed 那条；两条都在，不能只留成功路径
    assert [a["verb"] for a in harness.audits()] == ["seal", "publish"]


def test_E39_S43_01_undelivered_audit_is_not_counted_as_audited(h):
    """E39-S43-01 S45 不可达：意图留在本地 `delivered=0`，包状态不回滚。"""
    def sink_down(_tenant, intent):
        raise ServiceError(503, "AUDIT_SINK_UNAVAILABLE", "S45 down")

    harness = Harness(audit_sink=sink_down)
    package = harness.create()
    # 包确实封好了 —— 审计不可达不影响已提交的事实
    assert package["state"] == "SEALED"
    assert harness.count("audit_intent") == 1
    assert harness.audits()[0]["delivered"] == 0
    assert harness.audits()[0]["record_id"] is None
    assert harness.service.pending_audits(TENANT) == {
        "undelivered_audit_intents": 1}


def test_E39_S43_02_evidence_traces_back_to_its_sources(h):
    """E39-S43-02 从包能追到 policy decision / master candidate /
    rendering evidence，且给的是**引用**不是副本。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    package = h.create()
    h.publish(package["package_id"])

    evidence = h.service.get_evidence(TENANT, package["package_id"])
    assert evidence["master_candidate_id"] == "mc1"
    assert evidence["master_artifact_ref"]["id"] == "m1"
    assert evidence["timeline_ref"] == {"id": "tl1", "revision": 4}
    assert evidence["manifest_ref"] == "man1"

    # 授权链：policy decision id 与 hash 与调用方给的那张引用逐字一致
    assert len(evidence["policy_refs"]) == 1
    ref = evidence["policy_refs"][0]
    assert ref["policy_decision_id"] == PB["publish_authorization_ref"][
        "policy_decision_id"]
    assert ref["decision_hash"] == DH
    assert ref["verdict"] == "allow"
    # 审计意图也一并可查
    assert [a["action"] for a in evidence["audit_intents"]] == [
        "delivery.package.sealed", "delivery.package.published"]

    # 证据只带 external_ref，不复制上游报告正文（S37/S02/S16/S09/S23）
    by_role = {item["role"]: item for item in evidence["items"]}
    assert by_role["qc_evidence"]["external_ref"] == {
        "service_id": "S37", "report_id": "r1"}
    assert by_role["policy_snapshot"]["external_ref"] == {
        "service_id": "S02", "decision_id": "pd1"}
    assert by_role["memory_checkpoint"]["external_ref"]["service_id"] == "S16"
    assert by_role["asset_bindings"]["external_ref"]["service_id"] == "S09"
    assert by_role["cost_snapshot"]["external_ref"]["service_id"] == "S23"
    # 固定项不含上游内容字段：没有 check 列表、没有报价明细
    for role in ("qc_evidence", "policy_snapshot"):
        assert "checks" not in by_role[role]["external_ref"]
        assert "amounts" not in by_role[role]["external_ref"]


def test_E39_S43_03_audit_survives_package_deletion(h):
    """E39-S43-03 删除包（保留要求）后，审计仍能回答"发生过什么"。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    package = h.create()
    h.publish(package["package_id"])
    package_id = package["package_id"]
    assert len(h.audits()) == 2

    # 保留要求的等价物：先删包及其从属，再删包本身
    for table in ("receipt_callback", "receipt", "distribution_attempt",
                  "publish_authorization_ref", "integrity_signature",
                  "manifest", "item"):
        h.conn.execute(f"DELETE FROM {table} WHERE tenant_id=?", (TENANT,))
    h.conn.execute("DELETE FROM package WHERE tenant_id=?", (TENANT,))
    assert h.count("package") == 0

    # `audit_intent` 没有指向 package 的外键，也不跟着删
    surviving = h.audits()
    assert [(a["verb"], a["action"], a["outcome"]) for a in surviving] == [
        ("seal", "delivery.package.sealed", "SUCCEEDED"),
        ("publish", "delivery.package.published", "SUCCEEDED"),
    ]
    assert all(a["package_id"] == package_id for a in surviving)
    # 事件也在：outbox 不被包删除牵连
    assert h.event_types().count("delivery.package.published") == 1


# ------------------------------------------------ C-S43 通用与兼容性指标


def test_C_S43_01_manifest_hash_is_stable_across_100_recomputations(h):
    """C-S43-01 同一 manifest 算 100 次 hash 一致，`canonical_version` 固定。"""
    package = h.create()
    package_id = package["package_id"]
    frozen = h.service.get_manifest(TENANT, package_id)
    manifest = frozen["manifest"]
    version = frozen["canonical_version"]
    assert version == CANONICAL_VERSION
    assert h.packaged(package_id)["canonical_version"] == version

    hashes = {manifest_hash_for(manifest, version) for _ in range(100)}
    assert len(hashes) == 1
    assert hashes.pop() == frozen["manifest_hash"]
    # 走服务接口重算也一样（不是只有本地函数稳定）
    for _ in range(10):
        assert h.service.verify_manifest(TENANT, package_id)["valid"] is True


def test_C_S43_01_hash_covers_the_canonical_version(h):
    """C-S43-01 规范化版本进 hash：换个版本名字就是另一个 hash。"""
    package = h.create()
    manifest = h.service.get_manifest(TENANT, package["package_id"])["manifest"]
    assert manifest_hash_for(manifest, "jcs-v1") != \
        manifest_hash_for(manifest, "project-spec-v2")


def test_C_S43_02_package_content_is_append_only(h):
    """C-S43-02 包不可变：没有"改包内容"的路径，改内容只能新建包。"""
    first = h.create()
    package_id = first["package_id"]
    frozen_hash = h.service.get_manifest(TENANT, package_id)["manifest_hash"]
    items_before = [dict(row) for row in h.conn.execute(
        "SELECT role, ordinal, content_hash, byte_size FROM item"
        " WHERE tenant_id=? AND package_id=? ORDER BY role, ordinal",
        (TENANT, package_id))]

    # 同一批素材、改一个字节数 → 新命令、新包；旧包原封不动
    changed = body(command_id="c2")
    changed["items"] = [dict(item) for item in changed["items"]]
    changed["items"][0] = {**changed["items"][0], "byte_size": 101}
    second = h.create(changed)
    assert second["package_id"] != package_id
    assert h.service.get_manifest(TENANT, package_id)["manifest_hash"] == \
        frozen_hash
    items_after = [dict(row) for row in h.conn.execute(
        "SELECT role, ordinal, content_hash, byte_size FROM item"
        " WHERE tenant_id=? AND package_id=? ORDER BY role, ordinal",
        (TENANT, package_id))]
    assert items_after == items_before

    # 源码里 item / manifest / integrity_signature 只增不改
    source = "\n".join(path.read_text(encoding="utf-8")
                       for path in sorted(PKG_DIR.glob("*.py")))
    for table in ("item", "manifest", "integrity_signature"):
        assert f"UPDATE {table}" not in source, table
        assert f"DELETE FROM {table}" not in source, table
    # `item` 只有一处 INSERT：建包事务里的那一次
    assert source.count("INSERT INTO item") == 1
    # `package` 的 manifest_hash 只在 `state='DRAFTING'` 的那条 UPDATE 里写
    assert source.count("SET state='SEALED', manifest_hash=?") == 1


def test_C_S43_03_current_build_verifies_the_frozen_canonical_version(h):
    """C-S43-03 新版本必须能校验旧 `canonical_version` 冻结的清单。"""
    package = h.create()
    package_id = package["package_id"]
    frozen = h.service.get_manifest(TENANT, package_id)
    # 不传版本时用清单**自己**冻结的那个，不是"今天的默认值"
    result = h.service.verify_manifest(TENANT, package_id)
    assert result["canonical_version"] == frozen["canonical_version"]
    assert result["valid"] is True
    assert result["manifest_hash"] == frozen["manifest_hash"]
    # 显式传同一个版本也成立
    assert h.service.verify_manifest(
        TENANT, package_id, canonical_version=frozen["canonical_version"]
    )["valid"] is True
    # 不认识的版本 → 拒绝，而不是"按默认值算一个及格分"
    with pytest.raises(ServiceError) as caught:
        h.service.verify_manifest(TENANT, package_id,
                                  canonical_version="jcs-v99")
    assert (caught.value.status, caught.value.code) == (
        409, "UNSUPPORTED_CANONICAL_VERSION")


def test_C_S43_04_unknown_role_is_refused_everywhere(h):
    """C-S43-04 不认识的 item role：建包 422、校验 409、渠道配置 422。
    绝不静默丢弃。"""
    payload = body(items=[
        {"role": "master_video", "artifact_ref": {"id": "av1"},
         "content_hash": H, "byte_size": 100, "mime": "mp4_h264_aac"},
        {"role": "quantum_flux", "artifact_ref": {"id": "qf1"}},
    ])
    with pytest.raises(ServiceError) as caught:
        h.create(payload)
    assert (caught.value.status, caught.value.code) == (
        422, "UNKNOWN_ITEM_ROLE")
    assert h.count("package") == 0

    # 旧版本不认识新 role：把不认识的 role 塞进已冻结的清单 → 拒绝整包
    package = h.create()
    frozen = h.service.get_manifest(TENANT, package["package_id"])
    tampered = json.loads(json.dumps(frozen["manifest"]))
    tampered["items"].append({"role": "quantum_flux", "ordinal": 9,
                              "artifact_ref": None, "content_hash": None,
                              "byte_size": None, "mime": None,
                              "locale": None, "external_ref": None})
    h.conn.execute(
        "UPDATE manifest SET manifest=? WHERE tenant_id=? AND package_id=?",
        (json.dumps(tampered, ensure_ascii=False), TENANT,
         package["package_id"]))
    with pytest.raises(ServiceError) as caught:
        h.service.verify_manifest(TENANT, package["package_id"])
    assert (caught.value.status, caught.value.code) == (
        409, "UNKNOWN_ITEM_ROLE")

    # 渠道的 required_artifacts 也不接受未知 role
    with pytest.raises(ServiceError) as caught:
        h.service.put_channel(TENANT, "ch9",
                              {**CHAN, "required_artifacts": ["quantum_flux"]})
    assert (caught.value.status, caught.value.code) == (
        422, "UNKNOWN_ITEM_ROLE")


# ------------------------------------------------------- 通用验收：边界


def _source_files() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8")
            for path in sorted(PKG_DIR.glob("*.py"))}


def test_service_owns_exactly_its_own_tables(h):
    """只有它自己那 16 张表；`idempotency_record` 是平台 SDK 的。"""
    created = h.tables()
    assert "idempotency_record" in created        # SDK 建了，证明探针有效
    assert created - {"idempotency_record"} == OWNED_TABLES
    # 明确断言"这不是别人的库"：没有第二个权威的影子副本
    for foreign in ("balance", "accepted_pointer", "accepted_pointer_view",
                    "budget", "cost_ledger", "master", "artifact"):
        assert foreign not in created, foreign


def test_no_cross_service_imports_or_database_access():
    """AGENTS.md：不跨库、不 import 别人的业务 ORM。"""
    for name, source in _source_files().items():
        tree = ast.parse(source, filename=name)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                # 相对 import（level>0）是本包内部，不是别的服务
                imported.add(node.module if node.level == 0 else
                             f"delivery.{node.module}")
        allowed = {"__future__", "ast", "datetime", "hashlib", "json", "os",
                   "sqlite3", "time", "urllib", "uuid"}
        for module in imported:
            root = module.split(".")[0]
            assert root in allowed or root in (
                "delivery", "event_sdk", "idempotency_sdk"), (name, module)
    joined = "\n".join(_source_files().values())
    # 一个连接、一个库：没有第二个 sqlite 连接串，也没有跨库文件名
    assert joined.count("sqlite3.connect") == 0     # 连接由调用方注入
    for other in ("S42", "S38", "S37", "S28", "S08"):
        assert f'"{other}.db"' not in joined, other
        assert f"import {other.lower()}" not in joined, other


def test_no_hardcoded_provider_urls():
    """外部地址一律来自环境变量；源码里没有 `://` 字面量。"""
    for name, source in _source_files().items():
        assert "://" not in source, name
    app_source = APP_PATH.read_text(encoding="utf-8")
    assert "://" not in app_source


def test_external_publish_defaults_to_off():
    """本服务的默认值必须是"不发"，出站能力要靠显式开关。"""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    service = DeliveryService(conn)
    assert service.external_publish_enabled is False
    # 重发/外发相关依赖默认都不存在 → 调用即 503，而不是"没配置就放行"
    for attr in ("connector", "receipt_verifier", "signer"):
        with pytest.raises(ServiceError) as caught:
            getattr(service, attr)(TENANT, {})
        assert caught.value.status == 503
        assert caught.value.code.endswith("_UNAVAILABLE")


def test_unconfigured_dependencies_are_503_stubs_not_defaults():
    """`build_*(None)` 全部返回必抛 503 的桩，没有"假装成功"的兜底。"""
    expected = {
        "build_accepted_master_source": "ACCEPTED_MASTER_SOURCE_UNAVAILABLE",
        "build_artifact_source": "ARTIFACT_SOURCE_UNAVAILABLE",
        "build_timeline_source": "TIMELINE_SOURCE_UNAVAILABLE",
        "build_candidate_source": "CANDIDATE_SOURCE_UNAVAILABLE",
        "build_qc_source": "QC_SOURCE_UNAVAILABLE",
        "build_subtitle_source": "SUBTITLE_SOURCE_UNAVAILABLE",
        "build_rights_source": "RIGHTS_SOURCE_UNAVAILABLE",
        "build_policy_source": "POLICY_SOURCE_UNAVAILABLE",
        "build_memory_source": "MEMORY_SOURCE_UNAVAILABLE",
        "build_binding_source": "BINDING_SOURCE_UNAVAILABLE",
        "build_cost_source": "COST_SOURCE_UNAVAILABLE",
        "build_audit_sink": "AUDIT_SINK_UNAVAILABLE",
        "build_signer": "SIGNING_UNAVAILABLE",
        "build_connector": "CONNECTOR_UNAVAILABLE",
        "build_receipt_verifier": "RECEIPT_VERIFIER_UNAVAILABLE",
    }
    builders = {name for name in dir(deps) if name.startswith("build_")}
    assert builders == set(expected), builders ^ set(expected)
    for name, code in expected.items():
        stub = getattr(deps, name)(None)
        with pytest.raises(ServiceError) as caught:
            stub(TENANT, {})
        assert (caught.value.status, caught.value.code) == (503, code), name
        assert caught.value.retryable is False
        # 消息必须说出"缺的是哪个配置"，而不是一句笼统的失败
        message = str(caught.value)
        assert "configured" in message, message
        assert any(token in message for token in ("_BASE_URL", "S43",
                                                  "no QC report source")), \
            message


def test_qc_source_needs_all_three_qc_services():
    """QC 三件套缺一个就不算配置好 —— 空 dict 也是 503。"""
    for argument in (None, {}, {"S35": None, "S36": None, "S37": None},
                     {"S35": "http://x", "S36": None, "S37": "http://y"}):
        stub = deps.build_qc_source(argument)
        with pytest.raises(ServiceError) as caught:
            stub(TENANT, {})
        assert (caught.value.status, caught.value.code) == (
            503, "QC_SOURCE_UNAVAILABLE"), argument


def test_contract_gaps_are_registered_without_inventing_owner_decisions():
    assert len(CONTRACT_GAPS) == 34
    ids = [gap["id"] for gap in CONTRACT_GAPS]
    assert len(set(ids)) == len(ids), "CONTRACT_GAPS 里必须有唯一 id"
    for gap in CONTRACT_GAPS:
        assert set(gap) == {"handling", "id", "kind", "summary"}
        assert gap["kind"] in ("conflict", "gap", "needs_owner")
        assert gap["id"].startswith("S43-")
        assert gap["summary"] and gap["handling"]
    # 这几条是"必须登记、不许自己拍板"的：冲突、owner 决定、以及被删掉的
    # 编号不能凭空复活
    assert {"S43-35", "S43-26"} <= set(ids)
    assert "S43-33" not in ids and "S43-34" not in ids


def test_events_match_the_registered_topic_and_contract_schemas(h):
    """四条事件的 data 都要过 contracts 里的 schema（含负向对照）。"""
    jsonschema = pytest.importorskip("jsonschema")

    def check(payload, schema_file):
        schema = json.loads((CONTRACTS / schema_file).read_text(
            encoding="utf-8"))
        return list(jsonschema.Draft202012Validator(schema)
                    .iter_errors(payload))

    h.service.put_channel(TENANT, "ch1", CHAN)
    package = h.create()
    h.publish(package["package_id"])

    files = {
        "delivery.package.created": "delivery.package-created.v2.schema.json",
        "delivery.package.sealed": "delivery.package-sealed.v2.schema.json",
        "delivery.package.published":
            "delivery.package-published.v2.schema.json",
    }
    assert set(files) | {"delivery.publish.failed"} == set(EVENTS)
    for event_type, schema_file in files.items():
        payloads = h.event_payload(event_type)
        assert payloads, event_type
        for payload in payloads:
            assert check(payload, schema_file) == [], event_type

    # 失败事件：让连接器拒收，证明第四条也真的能发出且合规
    def denied(_tenant, request):
        raise ServiceError(400, "CHANNEL_REJECTED", "bad package")

    failed = Harness(connector=denied)
    failed.service.put_channel(TENANT, "ch1", CHAN)
    failed.publish(failed.create()["package_id"])
    failed_payloads = failed.event_payload("delivery.publish.failed")
    assert len(failed_payloads) == 1
    assert check(failed_payloads[0],
                 "delivery.publish-failed.v2.schema.json") == []

    # 负向对照：去掉一个必需键必须被抓住，否则上面的"通过"不算数
    broken = {k: v for k, v in failed_payloads[0].items()
              if k != "failure_code"}
    errors = check(broken, "delivery.publish-failed.v2.schema.json")
    assert any("failure_code" in error.message for error in errors)


def test_event_envelopes_use_the_registered_topic_and_monotonic_sequence(h):
    """聚合序号随包内事件单调 +1，`created` 恒为 1（§6.3）。"""
    h.service.put_channel(TENANT, "ch1", CHAN)
    package = h.create()
    h.publish(package["package_id"])
    events = h.events()
    assert [e["event_type"] for e in events] == [
        "delivery.package.created",
        "delivery.package.sealed",
        "delivery.package.published",
    ]
    sequences = [e["sequence"] for e in events]
    assert sequences == [1, 2, 3], sequences
    for event in events:
        envelope = event["envelope"]
        assert envelope["aggregate_id"] == package["package_id"]
        assert envelope["tenant_id"] == TENANT
        assert envelope["source"] == SOURCE
        assert envelope["schema_version"] == "2.0"
        assert envelope["data"]["schema_version"] == "2.0"


def test_restart_recovery_rebuilds_the_same_view(tmp_path):
    """E13 的另一半：进程重启后包、清单、attempt、审计都还在。"""
    path = tmp_path / "s43.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    first = Harness(conn)
    first.service.put_channel(TENANT, "ch1", CHAN)
    package = first.create()
    first.publish(package["package_id"])
    package_id = package["package_id"]
    before = dict(first.packaged(package_id))
    conn.close()

    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    second = Harness(conn)
    after = dict(second.packaged(package_id))
    assert after["state"] == before["state"] == "PUBLISHED"
    assert after["manifest_hash"] == before["manifest_hash"]
    assert after["package_hash"] == before["package_hash"]
    assert second.service.get_manifest(TENANT, package_id)["manifest_hash"] \
        == before["manifest_hash"]
    assert second.service.verify_manifest(TENANT, package_id)["valid"] is True
    assert second.count("distribution_attempt") == 1
    assert second.count("audit_intent") == 2
    # 幂等记录也持久：重投仍然命中同一结果，不会建第二个包
    replay = second.create()
    assert replay["replayed"] is True
    assert replay["package_id"] == package_id
    assert second.count("package") == 1
    # 渠道配置也活过来了
    assert second.service.list_channels(TENANT)["items"][0]["channel_id"] == "ch1"
    conn.close()


def test_execution_fence_and_node_epoch_live_in_separate_columns(h):
    """AGENTS.md：两个坐标分两列，永不互相赋值或比较。"""
    package = h.create()
    row = h.packaged(package["package_id"])
    assert row["execution_fence"] == 3
    assert row["node_epoch"] == 7
    assert package["execution_fence"] == 3
    assert package["node_epoch"] == 7

    # 源码层面：没有把一列写进另一列、也没有拿两列互相比较
    source = "\n".join(_source_files().values())
    for pattern in (r"node_epoch\s*=\s*\w*\[?[\"']?execution_fence",
                    r"execution_fence\s*=\s*\w*\[?[\"']?node_epoch",
                    r"node_epoch\s*[<>!=]=\s*.*execution_fence",
                    r"execution_fence\s*[<>!=]=\s*.*node_epoch"):
        assert not re.search(pattern, source), pattern
    # fence 的比较只发生在 fence 与 fence 之间
    assert 'header_fence != int(draft["execution_fence"])' in source
    # 本服务不持有任何"接受指针"表
    assert "accepted_pointer" not in h.tables()


def test_tenant_comes_only_from_the_signed_header():
    """请求体里的 tenant_id 只用于比对，绝不当作归属。"""
    app_source = APP_PATH.read_text(encoding="utf-8")
    # 服务调用一律用 header 里的 tenant，没有一处把 body 的 tenant 传下去
    assert 'payload.get("tenant_id")' in app_source      # 只出现在比对
    assert "TENANT_MISMATCH" in app_source
    # 内核没有任何"从 body 读 tenant"的入口签名
    service_source = (PKG_DIR / "service.py").read_text(encoding="utf-8")
    assert "body.get(\"tenant_id\")" not in service_source
    assert "body[\"tenant_id\"]" not in service_source


def test_events_and_views_do_not_carry_channel_credentials(h):
    """§6.3：事件不含签名内容、下载链接、渠道凭据。

    `endpoint_ref` 本身是**引用**（`secret_ref`），交给连接器去解析；S43 既
    不解析它、也不把它写进事件或证据视图。
    """
    h.service.put_channel(TENANT, "ch1", CHAN)
    package = h.create()
    h.publish(package["package_id"])

    evidence = h.service.get_evidence(TENANT, package["package_id"])
    rendered = json.dumps({"package": package, "evidence": evidence},
                          ensure_ascii=False)
    for forbidden in ("endpoint_ref", "secret_ref", "signature_b64",
                      "key_ref", "download_url"):
        assert forbidden not in rendered, forbidden

    # 事件里也没有：签名内容与渠道凭据只留在本库/查询接口
    for event in h.events():
        envelope = json.dumps(event["envelope"], ensure_ascii=False)
        for forbidden in ("endpoint_ref", "secret_ref", "signature_b64",
                          "key_ref"):
            assert forbidden not in envelope, (event["event_type"], forbidden)

    # 连接器拿到的是**引用**，不是解析后的凭据：S43 没有凭据可给
    sent = []

    def capture(tenant, request):
        sent.append(request)
        return {"outcome": "succeeded", "remote_id": "rem1", "bytes_sent": 110}

    harness = Harness(connector=capture)
    harness.service.put_channel(TENANT, "ch1",
                                {**CHAN, "endpoint_ref": {"secret_ref": "x"}})
    harness.publish(harness.create()["package_id"])
    assert len(sent) == 1
    assert sent[0]["endpoint_ref"] == {"secret_ref": "x"}


def test_service_defaults_to_no_external_publish():
    """内核默认不发（§10 末条）：没有显式打开，`publish` 就是 503。"""
    conn = sqlite3.connect(":memory:", check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    service = DeliveryService(conn)
    assert service.external_publish_enabled is False


# ================================================================ HTTP 层

def _client(tmp_path, monkeypatch, *, enabled=False):
    """按 S28/S37 的做法加载 `api/app.py`：**不** `import app`。

    模块级 `_conn` / `service` / `_idem` 在 import 时就建好了（走的是
    真实的 `build_*(env)` 桩）。这里把三个都换成 Harness 的闭包实现，才能
    在不联网的前提下走完整的 HTTP 路径。
    """
    pytest.importorskip("fastapi")
    import importlib.util

    from fastapi.testclient import TestClient

    monkeypatch.setenv("S43_DB", str(tmp_path / "s43.sqlite"))
    monkeypatch.setenv("EXTERNAL_PUBLISH_ENABLED", "false")
    for name in ("S08_BASE_URL", "S28_BASE_URL", "S41_BASE_URL",
                 "S42_BASE_URL", "S38_BASE_URL", "S15_BASE_URL",
                 "S02_BASE_URL", "S16_BASE_URL", "S09_BASE_URL",
                 "S23_BASE_URL", "S45_BASE_URL", "SIGNER_BASE_URL",
                 "CONNECTOR_BASE_URL", "RECEIPT_VERIFIER_BASE_URL",
                 "S35_BASE_URL", "S36_BASE_URL", "S37_BASE_URL"):
        monkeypatch.delenv(name, raising=False)

    spec = importlib.util.spec_from_file_location("s43_app_under_test",
                                                  APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    harness = Harness(enabled=enabled)
    module.service = harness.service
    module._idem = harness._idem
    module._conn = harness.conn
    return TestClient(module.app), harness


HTTP = {"Idempotency-Key": "c1", "X-Tenant-Id": TENANT}


def test_http_create_seal_query_and_readiness(tmp_path, monkeypatch):
    """201 建包 → 查询 / 清单 / 校验 / 证据 / attempt 全链路。"""
    client, harness = _client(tmp_path, monkeypatch)

    created = client.post("/delivery-packages", json=body(), headers=HTTP)
    assert created.status_code == 201, created.text
    payload = created.json()
    assert payload["replayed"] is False
    assert payload["state"] == "SEALED"
    assert payload["deliverable"] is True
    package_id = payload["package_id"]

    # 重放：同一 command、同一 body → 同一个包、标 replayed
    again = client.post("/delivery-packages", json=body(), headers=HTTP)
    assert again.status_code == 201
    assert again.json()["replayed"] is True
    assert again.json()["package_id"] == package_id

    got = client.get(f"/delivery-packages/{package_id}", headers=HTTP)
    assert got.status_code == 200
    assert got.json()["package_id"] == package_id

    listed = client.get("/delivery-packages", headers=HTTP)
    assert [item["package_id"] for item in listed.json()["items"]] == [
        package_id]

    manifest = client.get(f"/delivery-packages/{package_id}/manifest",
                          headers=HTTP)
    assert manifest.status_code == 200
    assert manifest.json()["manifest_hash"] == payload["manifest_hash"]

    verified = client.get(f"/delivery-packages/{package_id}/manifest/verify",
                          headers=HTTP)
    assert verified.status_code == 200
    assert verified.json()["valid"] is True

    evidence = client.get(f"/delivery-packages/{package_id}/evidence",
                          headers=HTTP)
    assert evidence.status_code == 200
    assert evidence.json()["state"] == "SEALED"
    assert evidence.json()["audit_intents"]

    attempts = client.get(f"/delivery-packages/{package_id}/attempts",
                          headers=HTTP)
    assert attempts.status_code == 200
    assert attempts.json()["attempts"] == []

    readiness = client.get("/health/readiness")
    assert readiness.status_code == 200
    assert readiness.json()["ready"] is True
    assert readiness.json()["service_id"] == "S43"
    # 默认关：HTTP 里没有任何东西把它打开
    assert readiness.json()["external_publish_enabled"] is False
    # 34 条未决项随巡检一起暴露，不是藏在 README 里
    assert len(readiness.json()["contract_gaps"]) == 34


def test_http_publish_is_202_and_reports_attempt_not_delivery(tmp_path,
                                                              monkeypatch):
    """`publish` 的 202 只表示"尝试过外发"，送达看 `state` / 回执。"""
    client, harness = _client(tmp_path, monkeypatch, enabled=True)
    package_id = client.post("/delivery-packages", json=body(),
                             headers=HTTP).json()["package_id"]
    # 渠道通过自有 API 建（默认 enabled=false，这里显式打开）
    put = client.post("/channels", json={"channel_id": "ch1", **CHAN},
                      headers={"X-Tenant-Id": TENANT})
    assert put.status_code == 200, put.text

    first = client.post(f"/delivery-packages/{package_id}/publish",
                        json=dict(PB),
                        headers={"Idempotency-Key": "pub1",
                                 "X-Tenant-Id": TENANT})
    assert first.status_code == 202, first.text
    assert first.json()["state"] == "SUCCEEDED"
    assert first.json()["dispatched"] is True
    # 已经 PUBLISHED 之后再发：不是 202（没有新外发），也不是错误
    second = client.post(f"/delivery-packages/{package_id}/publish",
                         json={"channel_id": "ch1", "command_id": "pub2"},
                         headers={"Idempotency-Key": "pub2",
                                  "X-Tenant-Id": TENANT})
    assert second.status_code == 200, second.text
    assert second.json()["dispatched"] is False
    assert "no second send" in second.json()["note"]
    # 连接器只被调了一次
    assert harness.calls["connector"] == 1


def test_http_publish_refuses_when_switches_are_off(tmp_path, monkeypatch):
    """两个开关默认都关：`publish` 是 503，不是"假装发过了"。

    这正是"外发不是送达"在 HTTP 层的落点——开关没开时连 attempt 都不该有。
    """
    client, harness = _client(tmp_path, monkeypatch, enabled=False)
    package_id = client.post("/delivery-packages", json=body(),
                             headers=HTTP).json()["package_id"]
    client.post("/channels", json={"channel_id": "ch1", **CHAN},
                headers={"X-Tenant-Id": TENANT})

    response = client.post(f"/delivery-packages/{package_id}/publish",
                           json=dict(PB),
                           headers={"Idempotency-Key": "pub1",
                                    "X-Tenant-Id": TENANT})
    assert response.status_code == 503, response.text
    assert response.json()["code"] == "CHANNEL_DISABLED"
    assert harness.calls["connector"] == 0
    assert harness.count("distribution_attempt") == 0

    # 渠道自己 disabled 也是 503：平台开关开着不代表这个渠道能用
    harness.service.external_publish_enabled = True
    harness.service.put_channel(TENANT, "ch1", {**CHAN, "enabled": False})
    blocked = client.post(f"/delivery-packages/{package_id}/publish",
                          json={**PB, "command_id": "pub2"},
                          headers={"Idempotency-Key": "pub2",
                                   "X-Tenant-Id": TENANT})
    assert blocked.status_code == 503
    assert blocked.json()["code"] == "CHANNEL_DISABLED"
    assert blocked.json()["details"]["channel_id"] == "ch1"
    assert harness.calls["connector"] == 0


def test_http_replay_and_conflict(tmp_path, monkeypatch):
    """同键同 body 重放；同键异 body 是 409，不是覆盖。"""
    client, _harness = _client(tmp_path, monkeypatch)
    first = client.post("/delivery-packages", json=body(), headers=HTTP)
    assert first.status_code == 201
    second = client.post("/delivery-packages", json=body(), headers=HTTP)
    assert second.json()["replayed"] is True

    changed = body(purpose="archive")
    conflict = client.post("/delivery-packages", json=changed, headers=HTTP)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    details = conflict.json()["details"]
    assert details["stored_request_hash"] != details["incoming_request_hash"]
    assert conflict.json()["retryable"] is False


def test_http_tenant_mismatch_is_403_and_cross_tenant_is_404(tmp_path,
                                                             monkeypatch):
    """租户只来自签名头：body 里对不上就 403；别人的包是 404（不是 403）。"""
    client, harness = _client(tmp_path, monkeypatch)

    in_context = body()
    in_context["context"]["tenant_id"] = OTHER
    assert client.post("/delivery-packages", json=in_context,
                       headers=HTTP).status_code == 403

    in_exec_context = body()
    in_exec_context["execution_context"] = {"tenant_id": OTHER}
    response = client.post("/delivery-packages", json=in_exec_context,
                           headers=HTTP)
    assert response.status_code == 403
    assert response.json()["code"] == "TENANT_MISMATCH"

    top_level = body(tenant_id=OTHER)
    assert client.post("/delivery-packages", json=top_level,
                       headers=HTTP).status_code == 403

    # 一个包都没建成：403 发生在落库之前
    assert harness.count("package") == 0

    package_id = client.post("/delivery-packages", json=body(),
                             headers=HTTP).json()["package_id"]
    missing = client.get("/delivery-packages/dp_does_not_exist",
                         headers=HTTP)
    other = client.get(f"/delivery-packages/{package_id}",
                       headers={"X-Tenant-Id": OTHER})
    assert missing.status_code == other.status_code == 404
    assert missing.json()["code"] == other.json()["code"] == \
        "PACKAGE_NOT_FOUND"
    # 连 message 都不该泄漏存在性
    assert "dp_does_not_exist" in missing.json()["message"]
    assert package_id in other.json()["message"]


def test_http_idempotency_key_must_match_the_body(tmp_path, monkeypatch):
    """头与 body 的 command_id 不一致是 400；缺头也是 400。"""
    client, harness = _client(tmp_path, monkeypatch)
    mismatched = client.post("/delivery-packages", json=body(),
                             headers={"Idempotency-Key": "other",
                                      "X-Tenant-Id": TENANT})
    assert mismatched.status_code == 400
    assert mismatched.json()["code"] == "INVALID_BODY"

    no_key = client.post("/delivery-packages", json=body(),
                         headers={"X-Tenant-Id": TENANT})
    assert no_key.status_code == 400

    no_tenant = client.post("/delivery-packages", json=body(),
                            headers={"Idempotency-Key": "c1"})
    assert no_tenant.status_code == 400

    assert harness.count("package") == 0


def test_http_in_flight_command_asks_for_a_retry(tmp_path, monkeypatch):
    """占位还在飞：409 + `Retry-After`，不是"没做过"也不是"做完了"。"""
    client, harness = _client(tmp_path, monkeypatch)
    harness._idem.begin(TENANT, "c1", stable_request_hash(body()))
    response = client.post("/delivery-packages", json=body(), headers=HTTP)
    assert response.status_code == 409
    assert response.headers["Retry-After"] == "1"
    assert response.json()["code"] == "COMMAND_IN_FLIGHT"
    assert response.json()["retryable"] is True
    assert harness.count("package") == 0


def test_http_channel_api_is_tenant_scoped_and_disabled_by_default(
        tmp_path, monkeypatch):
    """`POST/GET /channels`（S43-26）：不预填 enabled，且按租户隔离。"""
    client, _harness = _client(tmp_path, monkeypatch)
    put = client.post("/channels", json={"channel_id": "ch1", **CHAN,
                                         "enabled": None},
                      headers={"X-Tenant-Id": TENANT})
    assert put.status_code == 200, put.text
    assert put.json()["enabled"] is False

    mine = client.get("/channels", headers={"X-Tenant-Id": TENANT})
    assert [item["channel_id"] for item in mine.json()["items"]] == ["ch1"]
    theirs = client.get("/channels", headers={"X-Tenant-Id": OTHER})
    assert theirs.json()["items"] == []

    no_id = client.post("/channels", json={"kind": "partner_api"},
                        headers={"X-Tenant-Id": TENANT})
    assert no_id.status_code == 422
    assert no_id.json()["code"] == "CHANNEL_REQUIRED"

    mismatch = client.post("/channels", json={"channel_id": "ch2",
                                              "tenant_id": OTHER},
                           headers={"X-Tenant-Id": TENANT})
    assert mismatch.status_code == 403


def test_http_event_consumption_is_idempotent_and_tenant_scoped(
        tmp_path, monkeypatch):
    """`POST /events:consume`：inbox 去重；事件 tenant 对不上是 403。"""
    client, harness = _client(tmp_path, monkeypatch)
    envelope = {"event_id": "ev1", "type": "asset.voice.authorization.changed",
                "tenant_id": TENANT,
                "data": {"voice_id": "v1", "to_state": "revoked",
                         "state_seq": 5}}
    first = client.post("/events:consume", json={"event": envelope},
                        headers={"X-Tenant-Id": TENANT})
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] is True
    second = client.post("/events:consume", json={"event": envelope},
                         headers={"X-Tenant-Id": TENANT})
    assert second.json()["duplicate"] is True
    assert harness.count("inbox") == 1

    foreign = client.post("/events:consume",
                          json={"event": {**envelope, "tenant_id": OTHER}},
                          headers={"X-Tenant-Id": TENANT})
    assert foreign.status_code == 403
    assert foreign.json()["code"] == "TENANT_MISMATCH"


# -------------------------------------------------- 方案编号双向覆盖

def test_plan_ids_are_covered_both_ways():
    """方案 §9 的编号集合与本文件用例 docstring 的编号集合必须相等。

    方案里**没有 `T43-*`**（`PLAN_T43 == 0`）；用的是 E/C 编号。断言两个
    方向：方案有的这里都有，这里写的方案里都有。任何一边多一个都会失败，
    这样"加了用例忘了登记"和"方案加了项没实现"都能被抓住。
    """
    plan = PLAN_PATH.read_text(encoding="utf-8")
    plan_ids = set(CASE_ID.findall(plan))
    assert len(plan_ids) == 36, sorted(plan_ids)
    assert not re.search(r"T43-[A-Za-z0-9]", plan), "方案里居然有 T43-* 编号"

    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    tested = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("test_"):
            continue
        # 编号写在 docstring 第一行（用例名里也有，但 docstring 是"这条用例
        # 对应方案哪一项"的声明，用它比用函数名更不容易被改名带偏）
        tested.update(CASE_ID.findall(ast.get_docstring(node) or ""))

    assert not (plan_ids - tested), f"方案有、测试没有: {sorted(plan_ids - tested)}"
    assert not (tested - plan_ids), f"测试有、方案没有: {sorted(tested - plan_ids)}"
    assert tested == plan_ids


# === PART5 ===


def test_readiness_reports_unconfigured_upstreams_and_commands_really_503(
        tmp_path, monkeypatch):
    """默认部署（所有 `*_BASE_URL` 都没配）必须报 not ready。

    这是"分不清起得来和能用"的回归点：`ready` 曾只看 `SELECT 1`，
    于是一个 formal 包必然 503 的进程报 ready。两次探针要指同一件事——
    readiness 说哪些依缺少，创建就真的因它们 503。
    """
    client, _harness = _client(tmp_path, monkeypatch)
    # 把桩换回真实的 `build_*(None)`：这才是生产默认状态。
    # `_client` 已经把 module.service 换成 Harness 的闭包实现，这里重建一个
    # 全未配置的 service，验证它自报的家底与真实行为一致。
    from delivery.deps import (
        build_accepted_master_source, build_artifact_source,
        build_audit_sink, build_binding_source, build_candidate_source,
        build_connector, build_cost_source, build_memory_source,
        build_policy_source, build_qc_source, build_receipt_verifier,
        build_rights_source, build_signer, build_subtitle_source,
        build_timeline_source,
    )
    from delivery.service import DeliveryService

    unconfigured = DeliveryService(
        sqlite3.connect(":memory:", check_same_thread=False,
                        isolation_level=None),
        accepted_master_source=build_accepted_master_source(None),
        artifact_source=build_artifact_source(None),
        timeline_source=build_timeline_source(None),
        candidate_source=build_candidate_source(None),
        qc_source=build_qc_source({}),
        subtitle_source=build_subtitle_source(None),
        rights_source=build_rights_source(None),
        policy_source=build_policy_source(None),
        memory_source=build_memory_source(None),
        binding_source=build_binding_source(None),
        cost_source=build_cost_source(None),
        audit_sink=build_audit_sink(None),
        signer=build_signer(None),
        connector=build_connector(None),
        receipt_verifier=build_receipt_verifier(None),
        external_publish_enabled=False,
    )
    missing = unconfigured.unavailable_dependencies()
    assert missing, "全未配置却报不出任何缺失依赖"
    assert "ACCEPTED_MASTER_SOURCE_UNAVAILABLE" in missing

    # 同一件事的另一面：formal 包现在真的 503，且原因就是 G1 的 S08。
    with pytest.raises(ServiceError) as caught:
        unconfigured.create(TENANT, body()["command_id"], body(), H)
    assert caught.value.status == 503
    assert caught.value.code == "DEPENDENCY_UNAVAILABLE"
    assert caught.value.details["gate"] == "G1"
    assert caught.value.details["upstream_code"] in missing

    # 配齐的样子：Harness 的桩没有 `_unavailable_code`，所以报 ready。
    assert client.get("/health/readiness").json()["ready"] is True
    assert client.get("/health/readiness").json()["blockers"] == []
