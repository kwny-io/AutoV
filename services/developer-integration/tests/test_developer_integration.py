"""S46 developer-integration 测试。

不连真实上游、不发真实 HTTP、不查真实 DNS。每条测试对应
`plans/S46-developer-integration.md` §9 的一个验收 ID（T46-xx / E-number）；
方案与代码有出入时，断言的是**登记进 CONTRACT_GAPS 的那个行为**，并注明缺口。

三组：
  * 纯函数（JCS 摘要、签名、SSRF 判定、脱敏）—— 不碰库。
  * 内核（受理、转发、对账、凭据、订阅、投递）—— 内存 SQLite + 桩上游。
  * HTTP（路由、头、状态码）—— 用 `S46_DB` 指向临时文件后重新加载模块。
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
import time

import pytest

from developer_integration import (
    COMMANDS,
    CONTRACT_GAPS,
    EVENT_COMMAND_ACCEPTED,
    EVENT_TENANT_PARAM_IGNORED,
    SCOPES,
    SCHEMA,
    SCHEMA_VERSION,
    SOURCE,
    TOPIC,
    DeveloperIntegrationService,
    LocalTokenBuckets,
    ServiceError,
    UpstreamCall,
    UpstreamPort,
)
from developer_integration.redaction import redact_payload, redact_text
from developer_integration.schema import (
    EVENT_DATA_KEYS,
    FORWARD_BACKOFF_SECONDS,
    MAX_FORWARD_ATTEMPTS,
    MAX_KEYS_ACTIVE,
    MAX_KEYS_LIVE,
    MAX_WEBHOOK_ATTEMPTS,
    WEBHOOK_BACKOFF_SECONDS,
)
from developer_integration.service import (
    RateLimitBackendError,
    UnconfiguredUpstream,
    UpstreamUnknown,
    UpstreamUnavailable,
    _is_private_address,
    _open,
    _seal,
    sign_webhook,
    verify_webhook,
)

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))


# ------------------------------------------------------------------ 测试替身

class StubUpstream(UpstreamPort):
    """可编排的上游。**默认成功**，要失败得显式排队。"""

    def __init__(self, results=None, probe="unknown", state=None):
        self.calls: list[UpstreamCall] = []
        self.results = list(results or [])
        self.probe_result = probe
        self.state_result = state
        self.default = {"id": "run_1", "state": "queued",
                        "state_as_of": "2026-01-01T00:00:00.000Z",
                        "projection_watermark": 7}

    def call(self, request: UpstreamCall) -> dict:
        self.calls.append(request)
        if self.results:
            outcome = self.results.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return dict(self.default)

    def probe(self, *, tenant_id, service_id, upstream_ref, command_id):
        return self.probe_result

    def state(self, *, tenant_id, service_id, upstream_ref):
        return self.state_result


class FakeSender:
    """记录投递，按脚本返回状态码或抛连接错误。"""

    def __init__(self, codes=None):
        self.sent: list[dict] = []
        self.codes = list(codes or [])

    def __call__(self, *, url, body, headers):
        self.sent.append({"url": url, "body": body, "headers": headers})
        if self.codes:
            outcome = self.codes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return 200


def make_service(*, upstream=None, sender=None, resolver=None, clock=None,
                 **kwargs):
    conn = sqlite3.connect(":memory:", check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    service = DeveloperIntegrationService(
        conn, upstream=upstream if upstream is not None else StubUpstream(),
        webhook_sender=sender, resolver=resolver, clock=clock, **kwargs)
    return conn, service


def make_client(service, tenant="t1", *, scopes=("read", "execute", "admin"),
                name="acme", plan="standard", home_region="cn-east-1",
                home_cell="cell_a1"):
    client = service.create_client(tenant, name=name, plan=plan,
                                   home_region=home_region,
                                   home_cell=home_cell, scopes=scopes)
    key = service.create_api_key(tenant, client["api_client_id"])
    return client, key, service.verify_credential(key["api_key"])


def body(command="episode.generate", **overrides):
    payload = {"command": command,
               "target": {"project_id": "prj_1", "episode_id": "ep_1"},
               "params": {"generation_mode": "auto"},
               "client_trace_id": "trace-1"}
    payload.update(overrides)
    return payload


def public_dns(host):
    return ["93.184.216.34"]


# ------------------------------------------------------------------ 契约面

def test_t46_00_schema_and_events_match_the_contract():
    """事件 data 的键集合必须等于 `integration.command-accepted.v2` 的 required。"""
    assert tuple(sorted(EVENT_DATA_KEYS)) == (
        "api_client_id", "client_trace_id", "command", "operation_id",
        "principal_type", "receipt_id", "schema_version", "state", "target")
    assert SCHEMA_VERSION == "2.0"
    assert SOURCE == "S46"
    assert TOPIC == "drama.developer-integration.v2"


def test_t46_00_command_catalog_has_every_documented_command():
    """§2.3 的 12 条命令逐条存在，scope 与表一致（C46-2 / C46-4）。"""
    expected = {
        "episode.generate": ("S21", "execute", True),
        "run.get": ("S21", "read", False),
        "run.command": ("S21", "execute", False),
        "change.plan": ("S07", "plan", False),
        "change.apply": ("S07", "execute", True),
        "review.decide": ("S40", "review", False),
        "delivery.prepare": ("S43", "execute", False),
        "delivery.publish": ("S43", "execute", False),
        "memory.query": ("S16", "read", False),
        "context.preview": ("S18", "read", False),
        "policy.get": ("S02", "read", False),
        "cost.query": ("S23", "read", False),
    }
    assert set(COMMANDS) == set(expected)
    for name, (service_id, scope, admission) in expected.items():
        command = COMMANDS[name]
        assert command.upstream_service == service_id, name
        assert command.required_scope == scope, name
        assert command.requires_admission is admission, name
        assert command.required_scope in SCOPES, name


def test_t46_00_every_contract_gap_is_well_formed():
    ids = [gap["id"] for gap in CONTRACT_GAPS]
    assert len(ids) == len(set(ids)), "gap ids must be unique"
    assert set(ids) >= {"C46-1", "C46-2", "C46-5"}
    for gap in CONTRACT_GAPS:
        assert set(gap) == {"id", "kind", "summary", "handling"}
        assert gap["kind"] in ("blocking", "conflict", "gap", "decision")


# ------------------------------------------------------------------ 凭据

def test_t46_39_api_key_plaintext_is_returned_once_and_never_stored():
    conn, service = make_service()
    client = service.create_client("t1", name="acme", scopes=("read",))
    key = service.create_api_key("t1", client["api_client_id"])
    plaintext = key["api_key"]
    assert plaintext.startswith("ak_live_")
    stored = conn.execute("SELECT * FROM api_key").fetchone()
    blob = " ".join(str(v) for v in dict(stored).values())
    assert plaintext not in blob
    assert len(stored["secret_hash"]) == 64
    # `list_api_keys` 不回显明文，也不回显 hash。
    listed = service.list_api_keys("t1", client["api_client_id"])
    assert "api_key" not in json.dumps(listed)
    assert plaintext not in json.dumps(listed)


def test_t46_40_key_rotation_keeps_retiring_key_working_until_retire_at():
    _, service = make_service()
    client, key1, auth1 = make_client(service, scopes=("read",))
    # 第 2 个 ACTIVE：允许（上限 2）。
    key2 = service.create_api_key("t1", client["api_client_id"])
    retired = service.retire_api_key("t1", key1["kid"])
    assert retired["state"] == "RETIRING"
    # RETIRING 且未到期：仍然可用——零停机轮换的全部意义。
    assert service.verify_credential(key1["api_key"]).tenant_id == "t1"
    assert service.verify_credential(key2["api_key"]).tenant_id == "t1"
    # 再建第 3 个 ACTIVE：live 数变成 3 但 active 已是 1，允许。
    key3 = service.create_api_key("t1", client["api_client_id"])
    assert key3["state"] == "ACTIVE"
    # 第 4 个：active 已满 2 且 live 已满 3 → 409。
    with pytest.raises(ServiceError) as exc:
        service.create_api_key("t1", client["api_client_id"])
    assert exc.value.code == "KEY_LIMIT_REACHED"
    assert exc.value.status == 409
    revoke = service.revoke_api_key("t1", key3["kid"])
    assert revoke["state"] == "REVOKED"
    with pytest.raises(ServiceError) as exc:
        service.verify_credential(key3["api_key"])
    assert exc.value.code == "UNAUTHENTICATED"


def test_t46_41_key_limit_is_two_active_one_retiring():
    _, service = make_service()
    client = service.create_client("t1", name="acme", scopes=("read",))
    first = service.create_api_key("t1", client["api_client_id"])
    service.create_api_key("t1", client["api_client_id"])
    assert MAX_KEYS_ACTIVE == 2 and MAX_KEYS_LIVE == 3
    with pytest.raises(ServiceError) as exc:
        service.create_api_key("t1", client["api_client_id"])
    assert exc.value.code == "KEY_LIMIT_REACHED"
    # 退一个就又能建一个。
    service.retire_api_key("t1", first["kid"])
    service.create_api_key("t1", client["api_client_id"])


def test_t46_42_expired_retiring_key_is_revoked_on_use():
    """`retire_at` 过期后立刻失效；错误的 key 与过期的 key 都回 401。"""
    now = {"t": 1_800_000_000.0}

    def clock():
        from datetime import datetime, timezone
        return datetime.fromtimestamp(now["t"], tz=timezone.utc)

    _, service = make_service(clock=clock)
    client, key, _ = make_client(service, scopes=("read",))
    service.retire_api_key("t1", key["kid"], retire_days=14)
    now["t"] += 15 * 86400
    with pytest.raises(ServiceError) as exc:
        service.verify_credential(key["api_key"])
    assert exc.value.status == 401
    assert exc.value.code == "CREDENTIAL_EXPIRED"


def test_t46_43_sweep_expired_keys_revokes_only_expired_retiring():
    _, service = make_service()
    client, key, _ = make_client(service, scopes=("read",))
    active = service.create_api_key("t1", client["api_client_id"])
    service.retire_api_key("t1", key["kid"], retire_days=-1)
    assert service.sweep_expired_keys() == 1
    rows = {r["kid"]: r["state"] for r in service.conn.execute(
        "SELECT kid, state FROM api_key")}
    assert rows[key["kid"]] == "REVOKED"
    assert rows[active["kid"]] == "ACTIVE"


def test_t46_44_unknown_credential_is_401_and_tenant_comes_from_the_key():
    _, service = make_service()
    with pytest.raises(ServiceError) as exc:
        service.verify_credential("ak_live_nope")
    assert exc.value.code == "UNAUTHENTICATED"
    _, _, auth = make_client(service, tenant="tenant-a", scopes=("read",))
    assert auth.tenant_id == "tenant-a"


# ------------------------------------------------------------------ 受理 / 幂等

def test_t46_10_same_command_id_same_body_replays_without_duplicate_receipt():
    conn, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    first = service.accept_command(auth, body(), command_id="cmd-1")
    second = service.accept_command(auth, body(), command_id="cmd-1")
    assert second["replayed"] is True
    assert second["operation_id"] == first["operation_id"]
    count = conn.execute(
        "SELECT COUNT(*) AS n FROM integration_command_receipt").fetchone()["n"]
    assert count == 1


def test_t46_11_same_command_id_different_body_is_409_not_overwrite():
    conn, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    service.accept_command(auth, body(), command_id="cmd-1")
    changed = body(params={"generation_mode": "manual"})
    with pytest.raises(ServiceError) as exc:
        service.accept_command(auth, changed, command_id="cmd-1")
    assert exc.value.status == 409
    assert exc.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert exc.value.details["stored_request_hash"] != \
        exc.value.details["incoming_request_hash"]
    # 原回执没有被改写。
    row = conn.execute(
        "SELECT request_hash FROM integration_command_receipt").fetchone()
    assert row["request_hash"] == exc.value.details["stored_request_hash"]


def test_t46_12_client_supplied_request_hash_is_verified_not_trusted():
    _, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    payload = body()
    good = service.request_hash(payload)
    accepted = service.accept_command(
        auth, {**payload, "request_hash": good}, command_id="cmd-1")
    assert accepted["state"] == "ACCEPTED"
    with pytest.raises(ServiceError) as exc:
        service.accept_command(
            auth, {**payload, "request_hash": "0" * 64}, command_id="cmd-2")
    assert exc.value.status == 400
    assert exc.value.code == "REQUEST_HASH_MISMATCH"
    assert exc.value.details["computed"] == good


def test_t46_13_request_hash_ignores_trace_id_but_not_params():
    _, service = make_service()
    a = service.request_hash(body())
    b = service.request_hash(body(client_trace_id="trace-different"))
    c = service.request_hash(body(params={"generation_mode": "manual"}))
    assert a == b
    assert a != c


def test_t46_14_concurrent_double_accept_does_not_create_two_receipts():
    """`icr_cmd_uniq` 是最终防线，不依赖「先查后插」的时序。"""
    conn, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    first = service.accept_command(auth, body(), command_id="cmd-1")
    # 绕过内核的快速路径，直接再插一次：唯一约束必须挡住。
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO integration_command_receipt (receipt_id, tenant_id,"
            " operation_id, command_id, request_hash, api_client_id, kid,"
            " principal_type, command, target, params_sha256, params_bytes,"
            " state, accepted_at) VALUES ('r','t1','op2','cmd-1',?, 'c','k',"
            " 'api_client','run.get','{}',? ,0,'ACCEPTED','2026-01-01')",
            (first["upstream"]["ref"] and "0" * 64, "0" * 64))


def test_t46_15_acceptance_writes_receipt_and_outbox_in_one_transaction():
    conn, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt = service.accept_command(auth, body(), command_id="cmd-1")
    events = service.outbox.unpublished()
    assert len(events) == 1
    event = events[0]
    assert event["type"] == EVENT_COMMAND_ACCEPTED
    assert event["source"] == SOURCE
    assert event["tenant_id"] == "t1"
    assert event["aggregate_id"] == receipt["operation_id"]
    assert event["data"]["state"] == "ACCEPTED"
    assert set(event["data"]) == set(EVENT_DATA_KEYS)
    # 事件里没有 params 正文，也没有签名。
    serialized = json.dumps(event)
    assert "generation_mode" not in serialized


def test_t46_16_receipt_stores_only_a_digest_never_the_params():
    conn, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    secret_param = "the protagonist betrays the guild in act three"
    service.accept_command(
        auth, body(params={"generation_mode": "auto", "note": secret_param}),
        command_id="cmd-1")
    row = dict(conn.execute(
        "SELECT * FROM integration_command_receipt").fetchone())
    assert secret_param not in json.dumps(row)
    assert row["params_sha256"] and len(row["params_sha256"]) == 64
    assert row["params_bytes"] > 0
    # 也不允许出现一张「请求正文日志」表。
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "request_body_log" not in tables


def test_t46_17_tenant_id_derived_from_credential_not_from_body():
    """外部命令体里没有 tenant_id；出现即 400（硬规则，§2.2）。"""
    _, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    with pytest.raises(ServiceError) as exc:
        service.accept_command(auth, body(tenant_id="someone-else"),
                               command_id="cmd-1")
    assert exc.value.status == 400
    assert exc.value.code == "MALFORMED_REQUEST"
    assert "tenant_id" in exc.value.details["unknown_fields"]
    # 连 `actor` 也不接受。
    with pytest.raises(ServiceError) as exc:
        service.accept_command(auth, body(actor={"principal_id": "x"}),
                               command_id="cmd-1")
    assert exc.value.code == "MALFORMED_REQUEST"


def test_t46_18_mcp_supplied_tenant_is_overwritten_not_rejected():
    """LLM 给的 `tenant_id` 覆盖为认证值，记审计，**不报错**（§1.3 第 3 条）。"""
    conn, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt = service.accept_command(
        auth, body(params={"tenant_id": "evil-tenant",
                           "generation_mode": "auto"}),
        command_id="cmd-1", mcp=True)
    assert receipt["tenant_id_derived"] == "t1"
    events = service.outbox.unpublished()
    audit = [e for e in events if e["type"] == EVENT_TENANT_PARAM_IGNORED]
    assert len(audit) == 1
    assert audit[0]["data"]["supplied"] == "evil-tenant"
    assert audit[0]["data"]["applied"] == "t1"
    assert audit[0]["tenant_id"] == "t1"


def test_t46_19_expected_tenant_mismatch_is_403_without_leaking_tenant():
    _, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    with pytest.raises(ServiceError) as exc:
        service.accept_command(auth, body(), command_id="cmd-1",
                               expected_tenant="tenant-b")
    assert exc.value.status == 403
    assert exc.value.code == "TENANT_MISMATCH"
    assert "tenant-b" not in str(exc.value)
    assert exc.value.details == {}


def test_t46_20_unknown_command_is_404_not_403():
    _, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute", "admin"))
    with pytest.raises(ServiceError) as exc:
        service.accept_command(auth, body(command="budget.set"), command_id="c")
    assert exc.value.status == 404
    assert exc.value.code == "COMMAND_NOT_SUPPORTED"
    assert "budget.set" not in json.dumps(exc.value.details["supported_commands"])


def test_t46_21_malformed_body_is_400_with_the_allowed_field_list():
    _, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    for payload in ({}, {"command": "run.get"},
                    {"command": "run.get", "target": {}},
                    {"command": "run.get", "target": {"run_id": "r"},
                     "params": []}):
        with pytest.raises(ServiceError) as exc:
            service.accept_command(auth, payload, command_id="cmd-1")
        assert exc.value.status == 400
        assert exc.value.code == "MALFORMED_REQUEST"


# ------------------------------------------------------------------ scope / 准入

def test_t46_28_read_scope_can_never_trigger_a_paid_admission():
    """`read` 调 `episode.generate` → 403，且**上游一次都没被调**。"""
    upstream = StubUpstream()
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read",))
    with pytest.raises(ServiceError) as exc:
        service.submit_command(auth, body(), command_id="cmd-1")
    assert exc.value.status == 403
    assert exc.value.code == "INSUFFICIENT_SCOPE"
    assert exc.value.details["would_require_admission"] is True
    assert upstream.calls == []


def test_t46_29_read_commands_work_with_read_scope_and_stay_free():
    upstream = StubUpstream()
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read",))
    receipt, outcome = service.submit_command(
        auth, body(command="run.get", target={"run_id": "run_1"}),
        command_id="cmd-1")
    assert outcome.state == "FORWARDED"
    assert COMMANDS["run.get"].requires_admission is False
    assert upstream.calls[0].action == "runs.get"


def test_t46_30_missing_scope_reports_the_required_one():
    _, service = make_service()
    _, _, auth = make_client(service, scopes=("read",))
    with pytest.raises(ServiceError) as exc:
        service.accept_command(auth, body(command="review.decide", target={
            "project_id": "p1", "decision_id": "d1"}), command_id="cmd-1")
    assert exc.value.status == 403
    assert exc.value.details["required_scope"] == "review"


# ------------------------------------------------------------------ 转发 / 未知
def test_t46_31_upstream_success_records_ref_and_state_snapshot():
    upstream = StubUpstream()
    conn, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, outcome = service.submit_command(auth, body(), command_id="cmd-1")
    assert outcome.state == "FORWARDED"
    assert receipt["state"] == "FORWARDED"
    assert receipt["upstream"]["ref"]["id"] == "run_1"
    assert receipt["upstream"]["state"] == "queued"
    assert receipt["upstream"]["state_as_of"] is not None
    assert receipt["upstream"]["projection_watermark"] == 7
    # 转发不带 receipt 里的摘要当正文，而是受理时内存里的原文。
    assert upstream.calls[0].params == {"generation_mode": "auto"}
    assert upstream.calls[0].idempotency_key == "cmd-1"
    assert upstream.calls[0].execution_context["context"]["tenant_id"] == "t1"


def test_t46_32_upstream_5xx_is_retrying_and_scheduled():
    upstream = StubUpstream(results=[UpstreamUnavailable("boom")])
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, outcome = service.submit_command(auth, body(), command_id="cmd-1")
    assert outcome.state == "FORWARD_RETRYING"
    assert outcome.status == 502
    assert outcome.code == "UPSTREAM_UNAVAILABLE"
    row = service.conn.execute(
        "SELECT * FROM integration_command_receipt").fetchone()
    assert row["state"] == "FORWARD_RETRYING"
    assert row["next_attempt_at"] is not None
    assert row["attempt_count"] == 1


def test_t46_33_upstream_unknown_never_auto_retries():
    """`503 UPSTREAM_UNKNOWN` 进 FORWARD_UNKNOWN，`next_attempt_at` 必须为空。

    这是这类系统最常见的重复付费来源（§7.2 末段）：如果它也排重试，
    对账逻辑与重试逻辑会在同一条回执上打架。
    """
    upstream = StubUpstream(results=[UpstreamUnknown("gateway timeout")])
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, outcome = service.submit_command(auth, body(), command_id="cmd-1")
    assert outcome.state == "FORWARD_UNKNOWN"
    assert outcome.status == 503
    assert outcome.code == "UPSTREAM_UNKNOWN"
    row = service.conn.execute(
        "SELECT * FROM integration_command_receipt").fetchone()
    assert row["state"] == "FORWARD_UNKNOWN"
    assert row["next_attempt_at"] is None
    # 轮询 job 不会把它捞回来。
    assert service.forward_pending() == []
    # 只有一条上游调用；没有偷偷重发。
    assert len(upstream.calls) == 1
    types = [e["type"] for e in service.outbox.unpublished()]
    assert "integration.forward.unknown" in types


def test_t46_34_retrying_exhaustion_escalates_to_unknown():
    clock = {"t": 1_800_000_000.0}

    def tick():
        from datetime import datetime, timezone
        return datetime.fromtimestamp(clock["t"], tz=timezone.utc)

    upstream = StubUpstream(results=[UpstreamUnavailable("down")]
                            * MAX_FORWARD_ATTEMPTS)
    _, service = make_service(upstream=upstream, clock=tick)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, outcome = service.submit_command(auth, body(), command_id="cmd-1")
    assert outcome.state == "FORWARD_RETRYING"
    # 每次把时钟推过退避窗口，让轮询 job 真正捞到它。
    for delay in FORWARD_BACKOFF_SECONDS:
        clock["t"] += delay + 1
        service.forward_pending()
    row = service.conn.execute(
        "SELECT * FROM integration_command_receipt").fetchone()
    assert row["state"] == "FORWARD_UNKNOWN"
    assert row["attempt_count"] == MAX_FORWARD_ATTEMPTS
    assert len(upstream.calls) == MAX_FORWARD_ATTEMPTS
    # 耗尽后不再排期：`FORWARD_UNKNOWN` 只能靠对账。
    assert row["next_attempt_at"] is None


def test_t46_35_upstream_4xx_is_rejected_and_not_retried():
    upstream = StubUpstream(results=[ServiceError(422, "EPISODE_NOT_READY",
                                                  "episode is locked")])
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, outcome = service.submit_command(auth, body(), command_id="cmd-1")
    assert outcome.state == "REJECTED"
    assert outcome.status == 422
    assert outcome.code == "EPISODE_NOT_READY"
    row = service.conn.execute(
        "SELECT * FROM integration_command_receipt").fetchone()
    assert row["next_attempt_at"] is None
    assert service.forward_pending() == []


def test_t46_36_upstream_auth_failure_is_not_retried():
    upstream = StubUpstream(results=[ServiceError(401, "NOPE", "bad credential")])
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, outcome = service.submit_command(auth, body(), command_id="cmd-1")
    assert outcome.state == "REJECTED"
    assert outcome.code == "UPSTREAM_AUTH_FAILED"
    assert len(upstream.calls) == 1


def test_t46_37_unconfigured_upstream_is_unknown_not_unavailable():
    """没有适配器时**不许**说「确定没发生」——那是可以重提的信号。"""
    conn, service = make_service(upstream=UnconfiguredUpstream())
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, outcome = service.submit_command(auth, body(), command_id="cmd-1")
    assert outcome.state == "FORWARD_UNKNOWN"
    assert outcome.code == "UPSTREAM_UNKNOWN"


def test_t46_38_trace_tenant_404_is_identical_for_missing_and_foreign():
    """E28：跨租户与不存在同形。先试探上游再泄漏，是最常见的实现错误。"""
    upstream = StubUpstream(results=[ServiceError(404, "NOT_FOUND", "no run")])
    _, service = make_service(upstream=upstream)
    _, _, auth_a = make_client(service, tenant="tenant-a",
                               scopes=("read", "execute"), name="a")
    _, _, auth_b = make_client(service, tenant="tenant-b",
                               scopes=("read", "execute"), name="b")
    receipt, outcome = service.submit_command(
        auth_a, body(command="run.get", target={"run_id": "run_of_b"}),
        command_id="cmd-1")
    assert outcome.status == 404
    assert outcome.code == "NOT_FOUND"
    # tenant-b 用同一个 operation_id 也查不到 tenant-a 的回执。
    with pytest.raises(ServiceError) as exc:
        service.get_receipt("tenant-b", receipt["operation_id"])
    assert exc.value.status == 404
    assert exc.value.code == "NOT_FOUND"
    assert "tenant-a" not in str(exc.value)


# ------------------------------------------------------------------ 对账

def test_t46_45_reconcile_executed_marks_forwarded():
    upstream = StubUpstream(results=[UpstreamUnknown("timeout")],
                            probe="executed")
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, _ = service.submit_command(auth, body(), command_id="cmd-1")
    results = service.reconcile()
    assert results[0]["resolution"] == "forwarded"
    assert service.get_receipt("t1", receipt["operation_id"])["state"] == \
        "FORWARDED"


def test_t46_46_reconcile_only_resubmits_once():
    upstream = StubUpstream(
        results=[UpstreamUnknown("timeout"), {"id": "run_1", "state": "queued"}],
        probe="not_executed")
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, _ = service.submit_command(auth, body(), command_id="cmd-1")
    first = service.reconcile()
    assert first[0]["resolution"] == "retry"
    # 重提后同一条 receipt 回到 ACCEPTED，`reconcile_count` 已经是 1；
    # 再判成 not_executed 也不会二次重提。
    row = service.conn.execute(
        "SELECT * FROM integration_command_receipt").fetchone()
    assert row["reconcile_count"] == 1
    assert row["state"] == "ACCEPTED"
    assert row["attempt_count"] == 0
    # 上游已成功 → 转发一次即 FORWARDED（正文来自 `_pending`，回执里没有）。
    ep = service._pending.get(("t1", receipt["operation_id"]))
    assert ep is not None
    outcome = service.forward("t1", receipt["operation_id"], params=ep)
    assert outcome.state == "FORWARDED"
    assert len(upstream.calls) == 2
    # 第二次对账时它已不是 UNKNOWN，不会被再提一次。
    assert service.reconcile() == []


def test_t46_47_reconcile_unknown_stays_unknown():
    upstream = StubUpstream(results=[UpstreamUnknown("timeout")],
                            probe="unknown")
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, _ = service.submit_command(auth, body(), command_id="cmd-1")
    assert service.reconcile()[0]["resolution"] == "unknown"
    assert service.get_receipt("t1", receipt["operation_id"])["state"] == \
        "FORWARD_UNKNOWN"
    assert len(upstream.calls) == 1


def test_t46_48_reconcile_past_the_window_becomes_stale_unknown():
    clock = {"t": 1_800_000_000.0}

    def tick():
        from datetime import datetime, timezone
        return datetime.fromtimestamp(clock["t"], tz=timezone.utc)

    upstream = StubUpstream(results=[UpstreamUnknown("timeout")], probe="unknown")
    _, service = make_service(upstream=upstream, clock=tick)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, _ = service.submit_command(auth, body(), command_id="cmd-1")
    clock["t"] += 8 * 86400
    results = service.reconcile()
    assert results[0]["resolution"] == "stale"
    assert service.get_receipt("t1", receipt["operation_id"])["state"] == \
        "STALE_UNKNOWN"


# ------------------------------------------------------------------ 重启

def test_t46_49_restart_after_accept_before_forward_does_not_resend_empty_body():
    """E13：`ACCEPTED` 的回执在重启后不能拿空 params 重发。"""
    upstream = StubUpstream(results=[UpstreamUnknown("crash window")])
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, _ = service.submit_command(auth, body(), command_id="cmd-1")
    fresh = StubUpstream()
    restarted = DeveloperIntegrationService(service.conn, upstream=fresh)
    # 模拟回执仍是 ACCEPTED（受理成功、转发前崩溃）。
    service.conn.execute(
        "UPDATE integration_command_receipt SET state='ACCEPTED'"
        " WHERE operation_id=?", (receipt["operation_id"],))
    outcome = restarted.forward("t1", receipt["operation_id"])
    assert outcome.state == "FORWARD_UNKNOWN"
    assert fresh.calls == [], "must not call upstream with an empty body"


def test_t46_50_restart_preserves_receipts_and_replays_idempotently():
    _, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    first = service.submit_command(auth, body(), command_id="cmd-1")[0]
    restarted = DeveloperIntegrationService(service.conn, upstream=StubUpstream())
    replay = restarted.accept_command(auth, body(), command_id="cmd-1")
    assert replay["replayed"] is True
    assert replay["operation_id"] == first["operation_id"]


def test_t46_51_old_build_rejects_a_command_it_no_longer_knows():
    """回滚（E01）：代码里没有这条命令时，回执不被丢掉，标 REJECTED 说明原因。"""
    _, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt = service.accept_command(auth, body(), command_id="cmd-1")
    service.conn.execute(
        "UPDATE integration_command_receipt SET command='legacy.command'"
        " WHERE operation_id=?", (receipt["operation_id"],))
    outcome = service.forward("t1", receipt["operation_id"],
                              params={"generation_mode": "auto"})
    assert outcome.state == "REJECTED"
    assert outcome.status == 501
    assert outcome.code == "COMMAND_NOT_SUPPORTED"


# ------------------------------------------------------------------ 限流

def test_t46_52_tenant_rate_limit_is_429_with_retry_after():
    _, service = make_service()
    _, _, auth = make_client(service, scopes=("read",))
    service.conn.execute(
        "INSERT INTO tenant_quota_profile VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("t1", "standard", 2, 2, 2, 2, 1, 1, 1,
         "2026-01-01T00:00:00.000Z", "test"))
    service.accept_command(auth, body(command="run.get",
                                      target={"run_id": "r"}),
                           command_id="c1")
    service.accept_command(auth, body(command="run.get",
                                      target={"run_id": "r"}),
                           command_id="c2")
    with pytest.raises(ServiceError) as exc:
        service.accept_command(auth, body(command="run.get",
                                          target={"run_id": "r"}),
                               command_id="c3")
    assert exc.value.status == 429
    assert exc.value.code in ("TENANT_RATE_LIMITED", "ENDPOINT_FAMILY_LIMITED")
    assert exc.value.retryable is True
    assert exc.value.details["retry_after"] >= 1


def test_t46_53_noisy_neighbour_burns_its_own_bucket_only():
    """E35：一个租户打满，不影响另一个租户。"""
    _, service = make_service()
    _, _, auth_a = make_client(service, tenant="a", scopes=("read",), name="a")
    _, _, auth_b = make_client(service, tenant="b", scopes=("read",), name="b")
    for tenant, auth in (("a", auth_a), ("a", auth_a)):
        for i in range(6):
            try:
                service.accept_command(
                    auth, body(command="run.get", target={"run_id": "r"}),
                    command_id=f"{tenant}-{i}-{id(auth)}")
            except ServiceError as exc:
                assert exc.status == 429
    # b 仍然畅通。
    receipt = service.accept_command(
        auth_b, body(command="run.get", target={"run_id": "r"}),
        command_id="b-ok")
    assert receipt["state"] == "ACCEPTED"


def test_t46_54_rate_limit_backend_failure_fails_open():
    """限流器挂了要放行；授权挂了要拒绝（方向相反，§7.1）。"""
    class Broken(LocalTokenBuckets):
        def try_consume(self, *a, **k):
            raise RateLimitBackendError("redis down")

        def try_enter(self, *a, **k):
            raise RateLimitBackendError("redis down")

    _, service = make_service(rate_limiter=Broken())
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, outcome = service.submit_command(auth, body(), command_id="cmd-1")
    assert outcome.state == "FORWARDED"
    assert service.metrics.get("rate_limit_degraded") == 1


def test_t46_55_authorization_source_failure_fails_closed():
    def broken(**kwargs):
        raise ConnectionError("S01 down")

    _, service = make_service(authorization_source=broken)
    client = service.create_client("t1", name="acme", scopes=("read",))
    key = service.create_api_key("t1", client["api_client_id"])
    with pytest.raises(ServiceError) as exc:
        service.verify_credential(key["api_key"])
    assert exc.value.status == 503
    assert exc.value.retryable is True


def test_t46_56_authorization_source_revocation_takes_effect_immediately():
    revoked = {"scopes": ["read"], "tenant_state": "ACTIVE"}

    def source(**kwargs):
        return dict(revoked)

    _, service = make_service(authorization_source=source)
    client = service.create_client("t1", name="acme",
                                   scopes=("read", "execute"))
    key = service.create_api_key("t1", client["api_client_id"])
    auth = service.verify_credential(key["api_key"])
    assert auth.has("execute") is False  # 交集：S01 撤了就是没了
    assert auth.has("read") is True


def test_t46_57_scope_revocation_in_the_local_grant_is_immediate():
    _, service = make_service()
    client, _, _ = make_client(service, scopes=("read", "execute"))
    service.revoke_scope("t1", client["api_client_id"], "execute")
    with pytest.raises(ServiceError) as exc:
        service.grant_scope("t1", client["api_client_id"], "bogus")
    assert exc.value.code == "MALFORMED_REQUEST"
    client_view = service.get_client("t1", client["api_client_id"])
    assert "execute" not in client_view["scopes"]


# ------------------------------------------------------------------ 脱敏

def test_t46_58_redaction_happens_before_storage_and_never_rejects():
    conn, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, outcome = service.submit_command(
        auth, body(params={"generation_mode": "auto",
                           "provider": "sk-" + "a" * 40,
                           "db": "postgres://user:hunter2@db.internal/x"}),
        command_id="cmd-1")
    assert outcome.state == "FORWARDED"
    params = outcome and service._pending
    rows = dict(conn.execute(
        "SELECT * FROM integration_command_receipt").fetchone())
    assert rows["params_redacted"] == 1
    # 落库的摘要里不含明文。
    blob = json.dumps(rows)
    assert "hunter2" not in blob
    assert "a" * 40 not in blob


def test_t46_59_redaction_is_stable_and_scope_bound():
    from developer_integration.redaction import hmac_scope_key
    secret = b"k"
    token = "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.c2lnbmF0dXJlLXZhbHVl"
    first, counts = redact_text(token, "t1", secret=secret)
    second, _ = redact_text(token, "t1", secret=secret)
    assert first == second, "same value must tokenise identically"
    assert "REDACTED:hmac_" in first
    assert counts
    other, _ = redact_text(token, "t2", secret=secret)
    assert other != first, "tokens must be tenant-scoped"
    assert hmac_scope_key("t1", "api_key", secret) != \
        hmac_scope_key("t2", "api_key", secret)


def test_t46_60_class_b_redaction_is_not_linkable():
    cleaned, counts = redact_text(
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
        "t1", secret=b"k")
    assert cleaned == "REDACTED:private_key_material"
    assert counts["private_key_material"] == 1


def test_t46_61_content_hashes_are_exempt_from_the_generic_pattern():
    payload = {"content_hash": "a" * 64, "note": "sk-" + "b" * 30}
    cleaned, counts = redact_payload(payload, "t1", secret=b"k")
    assert cleaned["content_hash"] == "a" * 64
    assert cleaned["note"].startswith("REDACTED:")
    assert counts


# ------------------------------------------------------------------ 外部 ID

def test_t46_62_external_id_conflict_is_409_not_overwrite():
    _, service = make_service()
    _, _, auth = make_client(service, scopes=("read", "execute"))
    service.accept_command(
        auth, body(command="run.get", target={"run_id": "run_1"},
                   external_id="ORDER-1"), command_id="cmd-1")
    with pytest.raises(ServiceError) as exc:
        service.accept_command(
            auth, body(command="run.get", target={"run_id": "run_2"},
                       external_id="ORDER-1"), command_id="cmd-2")
    assert exc.value.status == 409
    assert exc.value.code == "EXTERNAL_ID_CONFLICT"


def test_t46_63_external_ids_are_tenant_scoped():
    _, service = make_service()
    _, _, auth_a = make_client(service, tenant="a", scopes=("read",),
                               name="client-a")
    _, _, auth_b = make_client(service, tenant="b", scopes=("read",),
                               name="client-b")
    for auth in (auth_a, auth_b):
        service.accept_command(
            auth, body(command="run.get", target={"run_id": "run_1"},
                       external_id="ORDER-1"),
            command_id=f"cmd-{auth.tenant_id}")
    with pytest.raises(ServiceError):
        service.resolve_external_id("c", "client-a", "ORDER-1")


# ------------------------------------------------------------------ Webhook

def test_t46_64_webhook_signature_is_independently_verifiable():
    secret = "whsec_" + "s" * 20
    raw = '{"id":"evt_1"}'
    header = sign_webhook(secret, "1800000000", raw)
    assert header.startswith("t=1800000000,v1=")
    assert verify_webhook(secret, header, raw, now_epoch=1800000000)
    assert not verify_webhook(secret, header, raw + " ", now_epoch=1800000000)
    assert not verify_webhook("other", header, raw, now_epoch=1800000000)


def test_t46_65_webhook_signature_rejects_stale_timestamp():
    secret = "whsec_" + "s" * 20
    header = sign_webhook(secret, "1800000000", "{}")
    assert not verify_webhook(secret, header, "{}", now_epoch=1800009999)


def test_t46_66_webhook_delivery_carries_signature_and_delivery_id():
    sender = FakeSender()
    _, service = make_service(sender=sender, resolver=public_dns)
    sub = service.create_subscription(
        "t1", url="https://example.com/hook",
        event_types=["orchestration.run.state-changed"],
        secret="whsec_" + "x" * 20)
    service.enqueue_event({"id": "evt_1", "type": "orchestration.run.state-changed",
                           "tenant_id": "t1", "aggregate_id": "run_1",
                           "data": {}})
    results = service.dispatch_deliveries()
    assert results[0]["state"] == "DELIVERED"
    sent = sender.sent[0]
    assert sent["headers"]["X-Drama-Delivery-Id"].startswith(
        f"{sub['subscription_id']}:evt_1:1")
    assert verify_webhook("whsec_" + "x" * 20,
                          sent["headers"]["X-Drama-Signature"], sent["body"])


def test_t46_67_webhook_secret_is_not_stored_in_plaintext_and_not_echoed():
    conn, service = make_service(resolver=public_dns)
    secret = "whsec_supersecretvalue123"
    view = service.create_subscription(
        "t1", url="https://example.com/hook", event_types=["audit.record.appended"],
        secret=secret)
    assert "secret" not in view
    assert view["secret_set"] is True
    blob = json.dumps(dict(conn.execute(
        "SELECT * FROM webhook_subscription").fetchone()))
    assert secret not in blob
    sealed = conn.execute(
        "SELECT secret_ref FROM webhook_subscription").fetchone()[0]
    assert sealed.startswith("local-v1:")
    assert _open(sealed, service.secret_key) == secret


def test_t46_68_webhook_secret_rotation_keeps_the_old_one_for_14_days():
    _, service = make_service(resolver=public_dns)
    sub = service.create_subscription(
        "t1", url="https://example.com/hook", event_types=["audit.record.appended"],
        secret="whsec_old")
    rotated = service.rotate_subscription_secret(
        "t1", sub["subscription_id"], "whsec_new")
    assert rotated["previous_secret_expires_at"]
    row = service.conn.execute(
        "SELECT * FROM webhook_subscription").fetchone()
    assert _open(row["secret_ref"], service.secret_key) == "whsec_new"
    assert _open(row["previous_secret_ref"], service.secret_key) == "whsec_old"


def test_t46_69_webhook_url_must_be_https():
    _, service = make_service(resolver=public_dns)
    with pytest.raises(ServiceError) as exc:
        service.create_subscription(
            "t1", url="http://example.com/hook",
            event_types=["audit.record.appended"], secret="whsec_x")
    assert exc.value.code == "INVALID_URL"


@pytest.mark.parametrize("url", [
    "https://127.0.0.1/hook",
    "https://10.0.0.5/hook",
    "https://192.168.1.1/hook",
    "https://169.254.169.254/latest/meta-data",
    "https://[::1]/hook",
    "https://0.0.0.0/hook",
])
def test_t46_70_ssrf_private_targets_are_blocked(url):
    """E29：创建时挡。DNS 可改，所以投递时还会再挡一次（下一条）。"""
    _, service = make_service()
    with pytest.raises(ServiceError) as exc:
        service.create_subscription(
            "t1", url=url, event_types=["audit.record.appended"],
            secret="whsec_x")
    assert exc.value.status == 400
    assert exc.value.code == "SSRF_BLOCKED"


def test_t46_71_ssrf_is_rechecked_at_delivery_time():
    """创建时解析到公网、投递时解析到私网 → 拒绝投递。"""
    resolution = {"addrs": ["93.184.216.34"]}
    sender = FakeSender()
    _, service = make_service(sender=sender,
                              resolver=lambda host: resolution["addrs"])
    service.create_subscription(
        "t1", url="https://rebind.example.com/hook",
        event_types=["audit.record.appended"], secret="whsec_x")
    service.enqueue_event({"id": "evt_1", "type": "audit.record.appended",
                           "tenant_id": "t1", "aggregate_id": "a1"})
    resolution["addrs"] = ["10.1.2.3"]
    results = service.dispatch_deliveries()
    assert results[0]["state"] == "FAILED"
    assert results[0]["reason"] == "SSRF_BLOCKED"
    assert sender.sent == []


def test_t46_72_private_address_helper_covers_reserved_ranges():
    for address in ("127.0.0.1", "10.1.1.1", "172.16.0.1", "192.168.0.1",
                    "169.254.169.254", "0.0.0.0", "::1", "fc00::1"):
        assert _is_private_address(address), address
    for address in ("93.184.216.34", "8.8.8.8", "2606:4700::1111"):
        assert not _is_private_address(address), address


def test_t46_73_delivery_is_serial_per_aggregate_partition():
    sender = FakeSender()
    _, service = make_service(sender=sender, resolver=public_dns)
    service.create_subscription(
        "t1", url="https://example.com/hook",
        event_types=["orchestration.run.state-changed"], secret="whsec_x")
    for index in range(3):
        service.enqueue_event({
            "id": f"evt_{index}", "type": "orchestration.run.state-changed",
            "tenant_id": "t1", "aggregate_id": "run_1"})
    first = service.dispatch_deliveries()
    # 一轮只投出同 partition 的第一条。
    assert len(first) == 1
    # 投递完成后才轮到下一条。
    second = service.dispatch_deliveries()
    assert len(second) == 1
    assert sender.sent[0]["body"] != sender.sent[1]["body"]


def test_t46_74_event_enqueue_is_idempotent_per_subscription():
    _, service = make_service(sender=FakeSender(), resolver=public_dns)
    service.create_subscription(
        "t1", url="https://example.com/hook",
        event_types=["audit.record.appended"], secret="whsec_x")
    event = {"id": "evt_1", "type": "audit.record.appended",
             "tenant_id": "t1", "aggregate_id": "a1"}
    assert service.enqueue_event(event) == 1
    assert service.enqueue_event(event) == 0
    count = service.conn.execute(
        "SELECT COUNT(*) FROM webhook_delivery").fetchone()[0]
    assert count == 1


def test_t46_75_delivery_retries_with_backoff_then_fails():
    sender = FakeSender(codes=[500] * MAX_WEBHOOK_ATTEMPTS)
    _, service = make_service(sender=sender, resolver=public_dns)
    service.create_subscription(
        "t1", url="https://example.com/hook",
        event_types=["audit.record.appended"], secret="whsec_x")
    service.enqueue_event({"id": "evt_1", "type": "audit.record.appended",
                           "tenant_id": "t1", "aggregate_id": "a1"})
    results = []
    # 把时钟推到每条退避之后，逐次重投。
    for _ in range(MAX_WEBHOOK_ATTEMPTS):
        service.conn.execute(
            "UPDATE webhook_delivery SET next_attempt_at='2000-01-01T00:00:00.000Z'")
        results.extend(service.dispatch_deliveries())
    assert results[-1]["state"] == "FAILED"
    assert len(sender.sent) == MAX_WEBHOOK_ATTEMPTS
    row = service.conn.execute("SELECT * FROM webhook_delivery").fetchone()
    assert row["state"] == "FAILED"
    assert row["attempt_count"] == MAX_WEBHOOK_ATTEMPTS
    assert len(WEBHOOK_BACKOFF_SECONDS) == MAX_WEBHOOK_ATTEMPTS


def test_t46_76_deleting_a_subscription_cancels_pending_deliveries():
    _, service = make_service(sender=FakeSender(), resolver=public_dns)
    sub = service.create_subscription(
        "t1", url="https://example.com/hook",
        event_types=["audit.record.appended"], secret="whsec_x")
    service.enqueue_event({"id": "evt_1", "type": "audit.record.appended",
                           "tenant_id": "t1", "aggregate_id": "a1"})
    result = service.delete_subscription("t1", sub["subscription_id"])
    assert result["cancelled_deliveries"] == 1
    state = service.conn.execute(
        "SELECT state FROM webhook_delivery").fetchone()[0]
    assert state == "CANCELLED"
    assert service.dispatch_deliveries() == []


def test_t46_77_stale_delivery_lease_is_requeued_without_bumping_attempts():
    sender = FakeSender()
    _, service = make_service(sender=sender, resolver=public_dns)
    sub = service.create_subscription(
        "t1", url="https://example.com/hook",
        event_types=["audit.record.appended"], secret="whsec_x")
    service.enqueue_event({"id": "evt_1", "type": "audit.record.appended",
                           "tenant_id": "t1", "aggregate_id": "a1"})
    service.conn.execute(
        "UPDATE webhook_delivery SET state='DELIVERING', attempt_count=1,"
        " lease_expires_at='2000-01-01T00:00:00.000Z'")
    assert service.requeue_stale_deliveries() == 1
    row = service.conn.execute("SELECT * FROM webhook_delivery").fetchone()
    assert row["state"] == "PENDING"
    assert row["attempt_count"] == 1
    # 活着的租约**不**被回收。
    service.conn.execute(
        "UPDATE webhook_delivery SET state='DELIVERING',"
        " lease_expires_at='2999-01-01T00:00:00.000Z'")
    assert service.requeue_stale_deliveries() == 0


def test_t46_78_subscription_cap_is_enforced():
    _, service = make_service(resolver=public_dns)
    for index in range(20):
        service.create_subscription(
            "t1", url=f"https://example.com/hook{index}",
            event_types=["audit.record.appended"], secret="whsec_x")
    with pytest.raises(ServiceError) as exc:
        service.create_subscription(
            "t1", url="https://example.com/one-more",
            event_types=["audit.record.appended"], secret="whsec_x")
    assert exc.value.status == 409
    assert exc.value.code == "WEBHOOK_LIMIT_REACHED"


def test_t46_79_unknown_event_type_is_rejected_at_subscription_time():
    _, service = make_service(resolver=public_dns)
    with pytest.raises(ServiceError) as exc:
        service.create_subscription(
            "t1", url="https://example.com/hook",
            event_types=["nope.not.registered"], secret="whsec_x")
    assert exc.value.code == "MALFORMED_REQUEST"


def test_t46_80_patch_cannot_change_the_secret():
    """HTTP 层拒绝改 secret（改 secret 等于新建），内核只认 rotate。"""
    _, service = make_service(resolver=public_dns)
    sub = service.create_subscription(
        "t1", url="https://example.com/hook",
        event_types=["audit.record.appended"], secret="whsec_x")
    updated = service.update_subscription(
        "t1", sub["subscription_id"], description="renamed")
    assert updated["description"] == "renamed"
    assert updated["secret_set"] is True


# ------------------------------------------------------------------ 查询

def test_t46_81_operation_view_always_carries_state_as_of():
    upstream = StubUpstream()
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, _ = service.submit_command(auth, body(), command_id="cmd-1")
    view = service.get_operation("t1", receipt["operation_id"], refresh=False)
    assert view["upstream"]["state_as_of"] is not None
    assert view["poll_url"].endswith(receipt["operation_id"])


def test_t46_82_operation_refreshes_upstream_state_but_keeps_receipt_state():
    upstream = StubUpstream()
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, _ = service.submit_command(auth, body(), command_id="cmd-1")
    # 让快照过期，然后上游换了状态。
    service.conn.execute(
        "UPDATE integration_command_receipt"
        " SET upstream_state_as_of='2000-01-01T00:00:00.000Z'")
    upstream.state_result = {"state": "succeeded",
                             "state_as_of": "2026-02-02T00:00:00.000Z",
                             "projection_watermark": 11}
    view = service.get_operation("t1", receipt["operation_id"])
    assert view["state"] == "FORWARDED"          # 回执状态，不冒充业务状态
    assert view["upstream"]["state"] == "succeeded"
    assert view["upstream"]["projection_watermark"] == 11


def test_t46_83_operation_refresh_failure_does_not_blank_the_snapshot():
    upstream = StubUpstream()
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, _ = service.submit_command(auth, body(), command_id="cmd-1")
    service.conn.execute(
        "UPDATE integration_command_receipt"
        " SET upstream_state_as_of='2000-01-01T00:00:00.000Z'")

    def boom(**kwargs):
        raise ConnectionError("S21 down")

    upstream.state = boom
    view = service.get_operation("t1", receipt["operation_id"])
    # 回执状态与最后一次快照都还在；`state_as_of` 没被清空、也没被伪造成
    # 「刚刚」。客户端靠它的年龄判断这条快照有多旧。
    assert view["state"] == "FORWARDED"
    assert view["upstream"]["state"] == "queued"
    assert view["upstream"]["state_as_of"] == "2000-01-01T00:00:00.000Z"


def test_t46_84_cancel_only_applies_to_retrying_operations():
    upstream = StubUpstream(results=[UpstreamUnavailable("down")])
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    receipt, _ = service.submit_command(auth, body(), command_id="cmd-1")
    cancelled = service.cancel_operation("t1", receipt["operation_id"])
    assert cancelled["state"] == "CANCELLED"
    # 已转发的不能本地取消——取消要走上游。
    upstream2 = StubUpstream()
    _, service2 = make_service(upstream=upstream2)
    _, _, auth2 = make_client(service2, scopes=("read", "execute"))
    receipt2, _ = service2.submit_command(auth2, body(), command_id="cmd-1")
    with pytest.raises(ServiceError) as exc:
        service2.cancel_operation("t1", receipt2["operation_id"])
    assert exc.value.status == 409


def test_t46_85_list_operations_is_tenant_scoped_and_filterable():
    upstream = StubUpstream(results=[ServiceError(422, "BAD", "nope")])
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    service.submit_command(auth, body(command="run.get",
                                      target={"run_id": "r"}),
                           command_id="cmd-1")
    received = service.list_operations("t1", state="REJECTED")
    assert len(received["operations"]) == 1
    assert service.list_operations("t2")["operations"] == []


# ------------------------------------------------------------------ 执行上下文

def test_t46_86_execution_context_is_tenant_bound_and_signed():
    upstream = StubUpstream()
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"),
                             home_region="cn-north-1", home_cell="cell_b2")
    service.submit_command(auth, body(), command_id="cmd-1")
    ctx = upstream.calls[0].execution_context
    payload = ctx["context"]
    assert payload["tenant_id"] == "t1"
    assert payload["home_region"] == "cn-north-1"
    assert payload["home_cell"] == "cell_b2"
    assert payload["command_id"] == "cmd-1"
    assert payload["actor"]["principal_id"] == auth.principal_id
    assert payload["actor"]["principal_type"] == "api_client"
    # 签名覆盖整个 context（含 tenant 与 actor）。
    assert ctx["signature"] and ctx["key_id"]
    from developer_integration.service import _jcs
    import hashlib
    import hmac
    expected = hmac.new(service.context_key, _jcs(payload).encode(),
                        hashlib.sha256).hexdigest()
    assert ctx["signature"] == expected


def test_t46_87_execution_context_passes_fence_and_epoch_through_untouched():
    """fence 属 run/branch、epoch 属单个 logical_node：S46 只透传，不推进。"""
    upstream = StubUpstream()
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"))
    service.submit_command(
        auth,
        body(context_ref={"execution_fence": 4, "node_epoch": 2,
                          "node_id": "shot_3"}),
        command_id="cmd-1")
    payload = upstream.calls[0].execution_context["context"]
    assert payload["execution_fence"] == 4
    assert payload["node_epoch"] == 2
    assert payload["node_id"] == "shot_3"


def test_t46_88_home_region_comes_from_the_client_row_not_a_header():
    """E31：外部请求只能命中自己 `home_region` 的服务。"""
    upstream = StubUpstream()
    _, service = make_service(upstream=upstream)
    _, _, auth = make_client(service, scopes=("read", "execute"),
                             home_region="cn-south-1", home_cell="cell_c9")
    # 伪造一个不同的区域：`AuthContext` 不读它，`verify` 之后仍是原值。
    assert auth.home_region == "cn-south-1"
    service.submit_command(auth, body(), command_id="cmd-1")
    assert upstream.calls[0].execution_context["context"]["home_region"] == \
        "cn-south-1"


# ------------------------------------------------------------------ HTTP 层

def _api(monkeypatch, tmp_path, *, upstream=None, sender=None, resolver=None):
    path = os.path.join(ROOT, "services", "developer-integration",
                        "api", "app.py")
    monkeypatch.setenv("S46_DB", str(tmp_path / "s46.db"))
    spec = importlib.util.spec_from_file_location("s46_api_app", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["s46_api_app"] = module
    spec.loader.exec_module(module)
    module.service.upstream = upstream if upstream is not None \
        else StubUpstream()
    module.service.webhook_sender = sender
    if resolver is not None:
        module.service.resolver = resolver
    from fastapi.testclient import TestClient
    return TestClient(module.app), module.service


def _seed(service, tenant="t1", scopes=("read", "execute", "admin")):
    client = service.create_client(tenant, name=f"client-{tenant}",
                                   scopes=scopes)
    key = service.create_api_key(tenant, client["api_client_id"])
    return client, key["api_key"]


def _hdrs(raw_key, tenant="t1", command_id="cmd-1"):
    return {"Authorization": f"Bearer {raw_key}",
            "X-Tenant-Id": tenant,
            "Idempotency-Key": command_id}


def test_t46_89_http_command_returns_202_and_poll_url(monkeypatch, tmp_path):
    api, service = _api(monkeypatch, tmp_path)
    _, raw = _seed(service)
    response = api.post("/integrations/commands", headers=_hdrs(raw),
                        json=body())
    assert response.status_code == 202, response.text
    payload = response.json()
    assert payload["state"] == "FORWARDED"
    assert payload["poll_url"].startswith("/operations/")
    assert payload["tenant_id_derived"] == "t1"
    assert "tenant_id" not in response.request.content.decode()


def test_t46_90_http_missing_credential_is_401_with_envelope(monkeypatch, tmp_path):
    api, _ = _api(monkeypatch, tmp_path)
    response = api.post("/integrations/commands",
                        json=body(), headers={"Idempotency-Key": "k"})
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHENTICATED"
    assert set(response.json()) >= {"code", "message", "retryable",
                                   "correlation_id", "details"}


def test_t46_91_http_expected_tenant_mismatch_is_403(monkeypatch, tmp_path):
    api, service = _api(monkeypatch, tmp_path)
    _, raw = _seed(service)
    headers = {**_hdrs(raw), "X-Expected-Tenant": "tenant-z"}
    response = api.post("/integrations/commands", headers=headers, json=body())
    assert response.status_code == 403
    assert response.json()["code"] == "TENANT_MISMATCH"
    assert "tenant-z" not in response.text


def test_t46_92_http_idempotency_replays_and_conflicts(monkeypatch, tmp_path):
    api, service = _api(monkeypatch, tmp_path)
    _, raw = _seed(service)
    first = api.post("/integrations/commands", headers=_hdrs(raw), json=body())
    second = api.post("/integrations/commands", headers=_hdrs(raw), json=body())
    assert first.status_code == second.status_code == 202
    assert second.json()["operation_id"] == first.json()["operation_id"]
    assert second.json()["replayed"] is True
    conflict = api.post("/integrations/commands", headers=_hdrs(raw),
                        json=body(params={"generation_mode": "manual"}))
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_t46_93_http_rate_limit_sets_retry_after(monkeypatch, tmp_path):
    # 固定窗口计数器按 `int(time.time())` 切桶（§6.5 明文取舍：跨界突发
    # 是允许的）。测试若跨秒运行，两次请求会落进两个窗口，计数各从 1 起，
    # 第二个请求就不会 429 —— 那不是内核 bug，是断言盯错了对象。把时钟
    # 钉在窗口中央，这条测试才只在测「超限 → 429 + Retry-After」。
    monkeypatch.setattr("developer_integration.service.time.time",
                        lambda: 1_767_225_600.5)
    api, service = _api(monkeypatch, tmp_path)
    _, raw = _seed(service)
    service.conn.execute(
        "INSERT INTO tenant_quota_profile VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("t1", "standard", 1, 1, 1, 1, 1, 1, 1,
         "2026-01-01T00:00:00.000Z", "test"))
    api.post("/integrations/commands", headers=_hdrs(raw, command_id="c1"),
             json=body(command="run.get", target={"run_id": "r"}))
    response = api.post("/integrations/commands",
                        headers=_hdrs(raw, command_id="c2"),
                        json=body(command="run.get", target={"run_id": "r"}))
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) >= 1
    assert response.json()["retryable"] is True


def test_t46_94_http_cross_tenant_operation_is_404(monkeypatch, tmp_path):
    api, service = _api(monkeypatch, tmp_path)
    _, raw_a = _seed(service, tenant="tenant-a", scopes=("read", "execute"))
    _, raw_b = _seed(service, tenant="tenant-b", scopes=("read",))
    created = api.post("/integrations/commands",
                       headers=_hdrs(raw_a, tenant="tenant-a"), json=body())
    operation_id = created.json()["operation_id"]
    response = api.get(f"/operations/{operation_id}",
                       headers=_hdrs(raw_b, tenant="tenant-b"))
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
    assert "tenant-a" not in response.text


def test_t46_95_http_read_scope_cannot_pay(monkeypatch, tmp_path):
    api, service = _api(monkeypatch, tmp_path)
    _, raw = _seed(service, scopes=("read",))
    response = api.post("/integrations/commands", headers=_hdrs(raw),
                        json=body())
    assert response.status_code == 403
    assert response.json()["code"] == "INSUFFICIENT_SCOPE"
    assert response.json()["details"]["required_scope"] == "execute"


def test_t46_96_http_webhook_endpoints_never_echo_the_secret(monkeypatch, tmp_path):
    api, service = _api(monkeypatch, tmp_path, resolver=public_dns)
    _, raw = _seed(service)
    created = api.post("/webhook-subscriptions", headers=_hdrs(raw),
                       json={"url": "https://example.com/hook",
                             "event_types": ["audit.record.appended"],
                             "secret": "whsec_verysecret"})
    assert created.status_code == 201, created.text
    assert "whsec_verysecret" not in created.text
    listed = api.get("/webhook-subscriptions", headers=_hdrs(raw))
    assert "whsec_verysecret" not in listed.text
    # PATCH 改 secret 必须被拒。
    patch = api.patch(
        f"/webhook-subscriptions/{created.json()['subscription_id']}",
        headers=_hdrs(raw), json={"secret": "whsec_new"})
    assert patch.status_code == 400


def test_t46_97_http_ssrf_is_400_through_the_api(monkeypatch, tmp_path):
    api, service = _api(monkeypatch, tmp_path)
    _, raw = _seed(service)
    response = api.post("/webhook-subscriptions", headers=_hdrs(raw),
                        json={"url": "https://169.254.169.254/x",
                              "event_types": ["audit.record.appended"],
                              "secret": "whsec_x"})
    assert response.status_code == 400
    assert response.json()["code"] == "SSRF_BLOCKED"


def test_t46_98_http_api_key_lifecycle_endpoints(monkeypatch, tmp_path):
    api, service = _api(monkeypatch, tmp_path)
    client, raw = _seed(service)
    created = api.post("/api-keys", headers=_hdrs(raw), json={})
    assert created.status_code == 201
    plaintext = created.json()["api_key"]
    assert plaintext.startswith("ak_live_")
    listed = api.get("/api-keys", headers=_hdrs(raw))
    assert plaintext not in listed.text
    retired = api.post(f"/api-keys/{created.json()['kid']}:retire",
                       headers=_hdrs(raw))
    assert retired.json()["state"] == "RETIRING"
    revoked = api.delete(f"/api-keys/{created.json()['kid']}",
                         headers=_hdrs(raw))
    assert revoked.json()["state"] == "REVOKED"


def test_t46_99_http_readiness_reports_unconfigured_dependencies(
        monkeypatch, tmp_path):
    api, _ = _api(monkeypatch, tmp_path)
    payload = api.get("/readiness").json()
    assert payload["ready"] is True
    assert payload["upstream_configured"] is True   # 桩已注入
    assert payload["webhook_sender"] is False        # 未注入
    assert payload["authorization_source"] is False
    assert {gap["id"] for gap in payload["contract_gaps"]} >= {"C46-1"}
