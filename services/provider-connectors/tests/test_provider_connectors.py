"""S27 provider-connectors 测试。编号对应 `plans/S27-provider-connectors.md` §15。

覆盖 docs/16 要求的 E12–E23 里属于 S27 的那一半：付款保护（无 grant 不发）、
重复投递幂等（两道门）、四态判定的最坏假设、乱序/缺失的回调、重启恢复、
渠道隔离。

## 环境

内存 SQLite，`isolation_level=None` + 显式 `BEGIN IMMEDIATE`（同 S21/S22/S23/
S26）。`transport` / `claim_grant` / `secret_for` 全是注入的假端口 —— 按
AGENTS.md「依赖服务未实现 → 用 mock 按契约走」。**没有一处真实出站 HTTP。**

## 与方案的偏差（逐条登记，测试只断言能证明的那一侧）

- **T27-25**：方案要"代码级单测：mapper 无此访问路径"。这里用 `ast` 扫描
  `mappers.py`：断言没有 `import os`、没有 `os.environ`、`to_native` 里没有对
  `model` 的赋值来自参数以外的东西。比"读一遍代码"可执行。
- **T27-26**：`connectors/*/*/connector.yaml` 的 `status` / `evidence_checked_at`
  是**仓库事实**，不是服务行为，但 §15 明列成断言，所以在这里扫文件。
- **T27-27**：`sora/historical` 没有 Deployment 属于部署层；本仓库能证明的是
  「没有注册它的适配器 → `UnknownChannel` → 不 ACK、不发事件」。
- **T27-29**：方案要"扫描日志断言无 prompt、无 key"。本服务不写日志（脱敏规则
  见 §14），所以断言的是**出站事件与回执里不含 prompt 原文**。
- **T27-30**：每渠道独立 secret 由 `secret_for` 端口保证。这里断言"删掉一个渠道
  的 secret 不影响另一个渠道"。
- **C27-4**：429 判 `not_sent`（不是 `unknown`）。§5.3 与 §13 在这里冲突，取 §13
  的"未受理、付费风险无"。测试断言 `not_sent` + `rate_limit` + `retry_after_ms`。
- **C27-6**：四个提交结论写在事件的 `status` 上，`provider_status_raw` 填
  `"SUBMIT_OUTCOME"`。契约没有 `error_class` / `retry_after_seconds` 的位置。
"""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path

import pytest

from provider_connectors import (
    FakeChannel,
    ProviderConnectorsService,
    ServiceError,
    TransportError,
    TransportResult,
    UnknownChannel,
    classify_submit,
    initialise,
)

ROOT = Path(__file__).resolve().parents[3]
SRC = Path(__file__).resolve().parents[1] / "src" / "provider_connectors"

TENANT = "t_alpha"
CHANNEL = "fake/channel"
SECRET = b"secret-alpha"


# ---------------------------------------------------------------- 夹具

def _conn():
    conn = sqlite3.connect(":memory:", check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    initialise(conn)
    return conn


class Harness:
    """一个服务 + 可编程的传输与 claim 端口。"""

    def __init__(self, *, transport=None, claim=None, channels=None,
                 secrets=None):
        self.conn = _conn()
        self.calls = []                  # 每次出站调用的 (op, request)
        self.claims = []
        self._transport = transport or (lambda op, req: TransportResult(200, {"id": "task_A"}))
        self._claim = claim or self._grant_ok
        self._secrets = {"fake/channel": SECRET} if secrets is None else secrets
        self.svc = ProviderConnectorsService(
            self.conn,
            channels=channels or [FakeChannel()],
            transport=self._record_transport,
            claim_grant=self._record_claim,
            secret_for=lambda cr: self._secrets.get(cr))

    def _record_transport(self, op, request):
        self.calls.append((op, request))
        return self._transport(op, request)

    def _record_claim(self, tenant_id, dispatch_id, body):
        self.claims.append((tenant_id, dispatch_id, body))
        return self._claim(tenant_id, dispatch_id, body)

    @staticmethod
    def _grant_ok(tenant_id, dispatch_id, body):
        return {"provider_job_id": "pj_" + dispatch_id,
                "stable_client_key": "dr_" + dispatch_id,
                "fencing_token": body["fencing_token"],
                "deadline": "2026-11-12T00:00:00.000000Z",
                "request_hash": body.get("request_hash") or "rh"}

    # ---- 构造事件 ------------------------------------------------------

    def submit_env(self, dispatch_id="dsp_1", *, event_id=None, channel=CHANNEL,
                   client_key=None, payload=None, **over):
        data = {
            "channel_route": channel,
            "dispatch_id": dispatch_id,
            "provider_job_id": "pj_" + dispatch_id,
            "stable_client_key": client_key or ("dr_" + dispatch_id),
            "fencing_token": 3,
            "request_hash": "rh",
            "provider_idempotency": "client_lookup_only",
            "tenant_id": TENANT,
            "correlation_id": "corr_1",
            "submit_payload_inline": payload if payload is not None else {
                "mode": "text_to_video", "duration_seconds": 5,
                "model_id": "fake", "model_snapshot": "v1",
                "prompt": {"text": "a cat"}},
        }
        data.update(over)
        return {"id": event_id or ("evt_" + dispatch_id),
                "type": "connector.submit.requested", "tenant_id": TENANT,
                "aggregate_id": dispatch_id, "correlation_id": "corr_1",
                "data": data}

    def status_env(self, provider_job_ref="task_A", *, dispatch_id="dsp_1"):
        return {"id": "evt_status_" + dispatch_id,
                "type": "connector.status.requested", "tenant_id": TENANT,
                "aggregate_id": "pj_" + dispatch_id, "correlation_id": "corr_1",
                "data": {"channel_route": CHANNEL,
                         "provider_job_id": "pj_" + dispatch_id,
                         "provider_job_ref": provider_job_ref,
                         "dispatch_id": dispatch_id,
                         "stable_client_key": "dr_" + dispatch_id}}

    # ---- 观察 ----------------------------------------------------------

    def events(self):
        return [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM outbox ORDER BY created_at, rowid")]

    def statuses(self):
        return [e["data"]["status"] for e in self.events()]

    def receipts(self):
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM connector_request_receipt ORDER BY created_at").fetchall()]

    def count(self, table):
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


def signed(body: bytes, ts: str, secret: bytes = SECRET) -> str:
    return hmac.new(secret, ts.encode("ascii") + b"." + body,
                    hashlib.sha256).hexdigest()


def callback_body(**over):
    body = {"id": "task_A", "state": "SUCCEEDED",
            "client_key": "dr_dsp_1"}
    body.update(over)
    return json.dumps(body).encode()


# ================================================================ 付款保护

def test_t27_01_submit_without_grant_never_sends():
    """T27-01：claim 拒绝 → `not_sent`，**没有出站调用**。"""
    from provider_connectors.service import ClaimUnavailable

    def deny(*_a):
        raise ClaimUnavailable("GATE_NOT_FOUND", 404)

    h = Harness(claim=deny)
    result = h.svc.consume(h.submit_env())

    assert result["outcome"] == "not_sent"
    assert result["claimed"] is False
    assert h.calls == [], "没有 grant 就不许发 HTTP"
    assert h.count("connector_request_receipt") == 0, "claim 前不许落回执"
    assert h.statuses() == ["not_sent"]


def test_t27_01b_grant_gate_is_compile_time_constant():
    """§5.2 门 1：`REQUIRE_DISPATCH_GRANT` 是常量，不是 env。"""
    from provider_connectors.service import REQUIRE_DISPATCH_GRANT
    assert REQUIRE_DISPATCH_GRANT is True

    tree = ast.parse((SRC / "service.py").read_text(encoding="utf-8"))
    assert not any(isinstance(n, ast.Import) and
                   any(a.name == "os" for a in n.names) for n in ast.walk(tree)), \
        "S27 不得 import os：门不能从环境变量来"


# ================================================================ 幂等两道门

def test_t27_02_same_dispatch_five_deliveries_only_one_http():
    """T27-02：同 `dispatch_id` 重投 5 次 → 1 次 HTTP、1 条 receipt。"""
    h = Harness()
    results = [h.svc.consume(h.submit_env("dsp_1")) for _ in range(5)]

    assert len(h.calls) == 1
    assert h.count("connector_request_receipt") == 1
    assert results[0]["applied"] is True
    assert all(r["applied"] is False for r in results[1:])
    assert all(r["reason"] == "DUPLICATE" for r in results[1:])

    # 换 event_id 重投也拦得住：回执是第二道门。
    again = h.svc.consume(h.submit_env("dsp_1", event_id="evt_other"))
    assert again["applied"] is False
    assert again["reason"] == "RECEIPT_EXISTS"
    assert len(h.calls) == 1


def test_t27_03_same_client_key_other_dispatch_is_refused():
    """T27-03：同一 `stable_client_key` 不同 `dispatch_id` → 冲突，拒绝。

    `stable_client_key` 以 S26 的 claim 返回值为准（它才是权威），所以这里让
    claim 对两个 dispatch 都回同一个 key —— 正是"派单方把同一个 key 派了两次"
    这个真实故障。
    """
    def same_key(tenant_id, dispatch_id, body):
        return {"provider_job_id": "pj_" + dispatch_id,
                "stable_client_key": "dr_shared",
                "fencing_token": body["fencing_token"],
                "deadline": "2026-11-12T00:00:00.000000Z",
                "request_hash": "rh"}

    h = Harness(claim=same_key)
    h.svc.consume(h.submit_env("dsp_1"))
    with pytest.raises(ServiceError) as exc:
        h.svc.consume(h.submit_env("dsp_2"))

    assert exc.value.code == "RECEIPT_CONFLICT"
    assert exc.value.status == 409
    assert len(h.calls) == 1, "第二个 dispatch 不该发出字节"
    assert h.count("connector_request_receipt") == 1


def test_t27_04_claim_conflict_does_not_submit():
    """T27-04：claim 返回 409（已领）→ 不 submit。"""
    from provider_connectors.service import ClaimUnavailable

    def conflict(*_a):
        raise ClaimUnavailable("GATE_ALREADY_CLAIMED", 409)

    h = Harness(claim=conflict)
    result = h.svc.consume(h.submit_env())

    assert result["outcome"] == "not_sent"
    assert result["error_class"] == "grant_unavailable"
    assert h.calls == []


def test_t27_05_claim_network_failure_then_success_only_one_http():
    """T27-05：claim 网络失败 3 次后才成功 → 只有成功之后的那 1 次 HTTP。

    用的是"同 event_id 反复投递"这条真实重投路径：失败时 `processed_at`
    留空，所以下一次能推进；成功后才写 `processed_at`。
    """
    attempts = {"n": 0}

    def flaky(*_a):
        attempts["n"] += 1
        if attempts["n"] <= 3:
            raise ConnectionError("claim endpoint unreachable")
        return Harness._grant_ok(*_a)

    h = Harness(claim=flaky)
    env = h.submit_env()

    for _ in range(3):
        with pytest.raises(ConnectionError):
            h.svc.consume(env)
        assert h.calls == [], "claim 没成功就不许发字节"

    result = h.svc.consume(env)
    assert result["applied"] is True
    assert len(h.calls) == 1
    assert h.count("connector_request_receipt") == 1

    # 再投一次：`processed_at` 已写，走 DUPLICATE。
    assert h.svc.consume(env)["reason"] == "DUPLICATE"
    assert len(h.calls) == 1


# ================================================================ 四态

@pytest.mark.parametrize("phase,expected", [
    ("dns", "not_sent"), ("connect", "not_sent"), ("tls", "not_sent"),
    ("pool", "not_sent"), ("build", "not_sent"),
])
def test_t27_06_pre_write_failures_are_not_sent(phase, expected):
    """T27-06 及其邻居：DNS/TCP/TLS/连接池/构造失败 → `not_sent`。

    `not_sent` 的定义是"能证明字节没离开本机"，所以只有这几个 phase 够格。
    """
    outcome, error_class = classify_submit(phase=phase)
    assert outcome == expected
    assert error_class == "transport"

    def boom(op, req):
        raise TransportError(phase)

    h = Harness(transport=boom)
    result = h.svc.consume(h.submit_env())
    assert result["outcome"] == "not_sent"
    assert h.receipts()[0]["bytes_written_at"] is None


@pytest.mark.parametrize("phase,expected_class", [
    ("timeout", "timeout"), ("reset", "transport"), ("partial", "transport"),
])
def test_t27_07_08_post_write_failures_are_unknown(phase, expected_class):
    """T27-07 / T27-08：写完之后的超时与重置一律 `unknown`。"""
    outcome, error_class = classify_submit(phase=phase)
    assert outcome == "unknown"
    assert error_class == expected_class

    def boom(op, req):
        raise TransportError(phase)

    h = Harness(transport=boom)
    result = h.svc.consume(h.submit_env())
    assert result["outcome"] == "unknown"
    receipt = h.receipts()[0]
    assert receipt["outcome"] == "unknown"
    # 写完之后才有 bytes_written_at —— 它不是 not_sent 的充分必要条件，
    # 但这一支里必须非空。
    assert receipt["bytes_written_at"] is not None


def test_t27_09_crash_recovery_marks_unknown_and_never_resends():
    """T27-09：恢复遗留 `SENDING` 一律 `unknown`，**哪怕 bytes_written_at 为 NULL**。"""
    h = Harness()
    h.conn.execute(
        "INSERT INTO connector_request_receipt (receipt_id, dispatch_id,"
        " provider_job_id, tenant_id, channel_route, stable_client_key,"
        " fencing_token, request_hash, state, request_bytes_sha256,"
        " bytes_written_at, created_at)"
        " VALUES ('r_crash','dsp_c','pj_dsp_c',? ,?, 'dr_dsp_c', 3, 'rh',"
        " 'SENDING', 'sha', NULL, '2026-11-12T00:00:00.000000Z')",
        (TENANT, CHANNEL))

    result = h.svc.recover_stale_sending()

    assert result["recovered"] == 1
    receipt = h.receipts()[0]
    assert receipt["state"] == "ABANDONED"
    assert receipt["outcome"] == "unknown"
    assert receipt["error_class"] == "crash_recovery"
    assert h.statuses() == ["unknown"]
    assert h.calls == [], "恢复不等于重发"


def test_t27_10_429_is_not_sent_with_retry_after(C27_4=None):
    """T27-10 + C27-4：429 → `not_sent` + `retry_after_ms`，不睡不重试。"""
    h = Harness(transport=lambda op, req: TransportResult(429, {},
                                                          retry_after_ms=30000))
    result = h.svc.consume(h.submit_env())

    assert result["outcome"] == "not_sent"
    assert result["error_class"] == "rate_limit"
    assert len(h.calls) == 1, "429 之后不许自动重试"
    health = h.svc.health(CHANNEL)
    assert health["breaker_state"] == "closed", "429 不熔断"
    assert health["retry_after_ms"] == 30000


def test_t27_11_5xx_is_unknown():
    """T27-11：供应商 5xx → `unknown`（任务可能已建）。"""
    h = Harness(transport=lambda op, req: TransportResult(502, {}))
    result = h.svc.consume(h.submit_env())
    assert result["outcome"] == "unknown"
    assert result["error_class"] == "provider_5xx"


def test_t27_12_2xx_without_ref_is_unknown():
    """T27-12：2xx 但解析不出 job ref → `unknown`，不是 accepted。"""
    h = Harness(transport=lambda op, req: TransportResult(200, {"ok": True}))
    result = h.svc.consume(h.submit_env())
    assert result["outcome"] == "unknown"
    assert result["error_class"] == "bad_response"
    assert h.receipts()[0]["provider_job_ref"] is None


def test_t27_13_401_opens_breaker_permanently():
    """T27-13：401 → `rejected/auth`，熔断 open 且**不自动恢复**。"""
    h = Harness(transport=lambda op, req: TransportResult(
        401, {"error": {"code": "unauthorized"}}))
    result = h.svc.consume(h.submit_env())

    assert result["outcome"] == "rejected"
    assert result["error_class"] == "auth"
    health = h.svc.health(CHANNEL)
    assert health["breaker_state"] == "open"
    assert health["retry_after_ms"] is None, "auth 引起的 open 等人处理"


def test_t27_14_policy_rejection_has_no_retry_and_no_channel_switch():
    """T27-14：政策拒绝 → `rejected/policy`，无重试、无换渠道。"""
    h = Harness(transport=lambda op, req: TransportResult(
        400, {"error": {"code": "content_policy"}}))
    result = h.svc.consume(h.submit_env())

    assert result["outcome"] == "rejected"
    assert result["error_class"] == "policy"
    assert len(h.calls) == 1
    assert h.count("connector_request_receipt") == 1


def test_t27_15_open_breaker_skips_claim_entirely():
    """T27-15：熔断打开时收到命令 → `not_sent`，**不 claim、不发 HTTP**。"""
    h = Harness(transport=lambda op, req: TransportResult(503, {}))
    for i in range(5):                    # 连续 5 次 provider_5xx → open
        h.svc.consume(h.submit_env(f"dsp_{i}"))
    assert h.svc.health(CHANNEL)["breaker_state"] == "open"

    claims_before = len(h.claims)
    calls_before = len(h.calls)
    result = h.svc.consume(h.submit_env("dsp_after"))

    assert result["outcome"] == "not_sent"
    assert result["error_class"] == "channel_circuit_open"
    assert result["claimed"] is False
    assert len(h.claims) == claims_before, "熔断打开时不许 claim"
    assert len(h.calls) == calls_before
    assert h.statuses()[-1] == "not_sent"


def test_breaker_half_open_after_window():
    """§8.1：30s 到点后转 half_open 放一个请求；成功即回 closed。"""
    h = Harness(transport=lambda op, req: TransportResult(500, {}))
    for i in range(5):
        h.svc.consume(h.submit_env(f"dsp_{i}"))
    assert h.svc.health(CHANNEL)["breaker_state"] == "open"

    # 把 opened_at 拨回窗口之外。
    h.conn.execute("UPDATE channel_health SET opened_at = ?"
                   " WHERE channel_route = ?",
                   ("2026-11-12T00:00:00.000000Z", CHANNEL))
    h.svc.transport = lambda op, req: TransportResult(200, {"id": "task_ok"})
    h._transport = h.svc.transport

    result = h.svc.consume(h.submit_env("dsp_after"))
    assert result["outcome"] == "accepted"
    assert h.svc.health(CHANNEL)["breaker_state"] == "closed"
    assert h.svc.health(CHANNEL)["consecutive_fail"] == 0


# ================================================================ 回调

def test_t27_16_forged_signature_is_401_and_never_stored():
    """T27-16：伪签名 → 401，**不进 Inbox**。"""
    h = Harness()
    body = callback_body()
    with pytest.raises(ServiceError) as exc:
        h.svc.receive_callback(CHANNEL, body,
                               {"x-timestamp": "1762900000",
                                "x-signature": "00" * 32},
                               now_epoch=1762900000.0)

    assert exc.value.status == 401
    assert exc.value.code == "CALLBACK_SIGNATURE_INVALID"
    assert h.count("connector_callback_inbox") == 0


def test_t27_16b_missing_headers_are_rejected():
    """§6.3：缺 `x-timestamp` / `x-signature` → 拒。"""
    h = Harness()
    with pytest.raises(ServiceError) as exc:
        h.svc.receive_callback(CHANNEL, callback_body(), {}, now_epoch=0.0)
    assert exc.value.details["reason"] == "missing_headers"
    assert h.count("connector_callback_inbox") == 0


def test_t27_17_timestamp_outside_window_is_rejected():
    """T27-17：±300s 之外 → 拒绝。"""
    h = Harness()
    ts = "1762900000"
    body = callback_body()
    with pytest.raises(ServiceError) as exc:
        h.svc.receive_callback(CHANNEL, body,
                               {"x-timestamp": ts, "x-signature": signed(body, ts)},
                               now_epoch=float(ts) + 301)
    assert exc.value.details["reason"] == "timestamp_out_of_window"

    # 恰好 300s 要通过（窗口是闭区间）。
    ok = h.svc.receive_callback(CHANNEL, body,
                                {"x-timestamp": ts, "x-signature": signed(body, ts)},
                                now_epoch=float(ts) + 300)
    assert ok["accepted"] is True


def test_t27_18_duplicate_callback_five_times_is_one_row():
    """T27-18：同一回调重投 5 次 → 1 条 Inbox、1 条观测。"""
    h = Harness()
    h.svc.consume(h.submit_env())          # 先有可关联的回执
    ts = "1762900000"
    body = callback_body()

    results = [h.svc.receive_callback(
        CHANNEL, body, {"x-timestamp": ts, "x-signature": signed(body, ts)},
        now_epoch=float(ts)) for _ in range(5)]

    assert results[0]["duplicate"] is False
    assert all(r["duplicate"] is True for r in results[1:])
    assert h.count("connector_callback_inbox") == 1

    h.svc.map_pending_callbacks(CHANNEL)
    h.svc.map_pending_callbacks(CHANNEL)   # 再扫一次不该重复发
    callback_events = [e for e in h.events() if e["data"]["source"] == "callback"]
    assert len(callback_events) == 1


def test_t27_19_callback_before_submit_lands_unmapped_then_maps():
    """T27-19：回调先于 submit 响应 → `UNMAPPED`，回执到达后映射并上报。"""
    body = callback_body(id="task_late", client_key="dr_dsp_1")
    ts = "1762900000"
    h = Harness(transport=lambda op, req: TransportResult(200, {"id": "task_late"}))

    # 回调先到：此时没有任何回执。
    h.svc.receive_callback(CHANNEL, body,
                           {"x-timestamp": ts, "x-signature": signed(body, ts)},
                           now_epoch=float(ts))
    assert h.svc.map_pending_callbacks(CHANNEL)["unmapped"] == 1
    assert h.conn.execute("SELECT state FROM connector_callback_inbox"
                          ).fetchone()["state"] == "UNMAPPED"
    assert not any(e["data"]["source"] == "callback" for e in h.events()), \
        "关联不上就不发观测（PCR-7：aggregate_id 不能空）"

    # 回执到了：`_on_submit` 末尾会回填。
    h.svc.consume(h.submit_env("dsp_1"))
    assert h.conn.execute("SELECT state FROM connector_callback_inbox"
                          ).fetchone()["state"] == "MAPPED"
    callback_events = [e for e in h.events() if e["data"]["source"] == "callback"]
    assert len(callback_events) == 1
    assert callback_events[0]["aggregate_id"] == "pj_dsp_1", \
        "aggregate_id 是平台侧 provider_job_id（PD-1）"


def test_t27_19b_callback_before_response_is_mapped_immediately_after_ack():
    """ACK-first：`receive_callback` 只落 Inbox，映射由后面的扫描驱动。"""
    h = Harness()
    h.svc.consume(h.submit_env())
    ts = "1762900000"
    body = callback_body()
    result = h.svc.receive_callback(
        CHANNEL, body, {"x-timestamp": ts, "x-signature": signed(body, ts)},
        now_epoch=float(ts))

    assert result["state"] == "RECEIVED"
    assert h.conn.execute("SELECT state FROM connector_callback_inbox"
                          ).fetchone()["state"] == "RECEIVED", \
        "ACK 那一刻还没映射"


def test_t27_20_out_of_order_callback_is_reported_as_is():
    """T27-20：乱序回调原样上报，由 S26 的 `PROGRESS_RANK` 拒绝倒退。"""
    h = Harness()
    h.svc.consume(h.submit_env())
    ts = "1762900000"

    for i, state in enumerate(["SUCCEEDED", "RUNNING"]):
        body = callback_body(state=state, id=f"task_{i}")
        h.svc.receive_callback(
            CHANNEL, body, {"x-timestamp": ts,
                            "x-signature": signed(body, ts)},
            now_epoch=float(ts))
    h.svc.map_pending_callbacks(CHANNEL)

    reported = [e["data"]["provider_status_raw"] for e in h.events()
                if e["data"]["source"] == "callback"]
    assert reported == ["SUCCEEDED", "RUNNING"], \
        "S27 不排序、不合并：它只上报看到的事实"


def test_t27_21_unmapped_past_24h_becomes_dead_letter():
    """T27-21：无法关联超 24h → `DEAD_LETTER`，**不丢弃**。"""
    h = Harness()
    ts = "1762900000"
    body = callback_body(id="task_orphan", client_key="dr_nowhere")
    h.svc.receive_callback(CHANNEL, body,
                           {"x-timestamp": ts, "x-signature": signed(body, ts)},
                           now_epoch=float(ts))

    h.conn.execute("UPDATE connector_callback_inbox SET received_at = ?",
                   ("2026-11-10T00:00:00.000000Z",))
    result = h.svc.map_pending_callbacks(CHANNEL)

    assert result["dead_letter"] == 1
    row = h.conn.execute("SELECT state, raw_body FROM connector_callback_inbox"
                         ).fetchone()
    assert row["state"] == "DEAD_LETTER"
    assert row["raw_body"], "原文必须留着，否则排障时无从下手"


# ================================================================ manifest

def test_t27_22_manifest_refresh_returns_new_urls():
    """T27-22：支持刷新的渠道 → 返回新 URL。"""
    manifest = [{"role": "video", "url": "https://cdn/new.mp4",
                 "sha256": "a" * 64, "bytes": 10, "content_type": "video/mp4"}]
    h = Harness(transport=lambda op, req: TransportResult(
        200, {"manifest": manifest}))

    env = {"id": "evt_mf", "type": "connector.manifest.requested",
           "tenant_id": TENANT, "aggregate_id": "pj_dsp_1",
           "correlation_id": "c",
           "data": {"channel_route": CHANNEL, "provider_job_id": "pj_dsp_1",
                    "provider_job_ref": "task_A", "dispatch_id": "dsp_1",
                    "stable_client_key": "dr_dsp_1", "refresh_urls": True}}
    result = h.svc.consume(env)

    assert result["applied"] is True
    assert result["refresh_supported"] is True
    assert result["manifest"] == manifest
    assert h.calls[0][0] == "manifest"


def test_t27_23_manifest_refresh_unsupported_says_so():
    """T27-23：不支持刷新 → `refresh_supported: false`，S26 走 §8.3。"""
    no_refresh = FakeChannel(channel_route="fake/no-refresh",
                             refresh_url_supported=False)
    h = Harness(channels=[no_refresh],
                secrets={"fake/no-refresh": SECRET})
    env = {"id": "evt_mf2", "type": "connector.manifest.requested",
           "tenant_id": TENANT, "aggregate_id": "pj_dsp_1",
           "correlation_id": "c",
           "data": {"channel_route": "fake/no-refresh",
                    "provider_job_id": "pj_dsp_1",
                    "provider_job_ref": "task_A", "dispatch_id": "dsp_1",
                    "stable_client_key": "dr_dsp_1", "refresh_urls": True}}
    result = h.svc.consume(env)

    assert result["applied"] is False
    assert result["refresh_supported"] is False
    assert h.calls == [], "不支持就别发请求"


# ================================================================ 映射器约束

def test_t27_24_unexpressible_capability_is_rejected():
    """T27-24：`required_capabilities` 里表达不了的一项必须报错。"""
    ch = FakeChannel()
    errors = ch.validate({"mode": "text_to_video", "duration_seconds": 5,
                          "required_capabilities": ["text_to_video",
                                                    "lip_sync_pro"]})
    codes = [e["code"] for e in errors]
    assert "CAPABILITY_UNEXPRESSIBLE" in codes
    # 表达得了的那项不能被误报。
    assert not any(e["path"] == "required_capabilities" and
                   "text_to_video" in e["detail"] for e in errors)


def test_t27_24b_validate_is_network_free_and_never_snaps_tiers():
    """§3.2：`validate` 纯本地；档外时长报错而**不吸附**。"""
    h = Harness(transport=lambda op, req: (_ for _ in ()).throw(
        AssertionError("validate 不许发网络")))
    result = h.svc.validate(CHANNEL, {"mode": "text_to_video",
                                      "duration_seconds": 7})
    assert result["ok"] is False
    assert result["errors"][0]["code"] == "DURATION_TIER_MISMATCH"
    assert h.calls == []


def test_t27_25_mapper_cannot_read_config_to_change_model():
    """T27-25：代码级断言 —— 映射器没有"读配置改 model"的路径。

    `ast` 扫三件事：不 import `os`；不出现 `os.environ` / `getenv`；
    `to_native` 里对 `model` 的赋值来源只有 `payload`。
    """
    tree = ast.parse((SRC / "mappers.py").read_text(encoding="utf-8"))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "os" not in imported, "mappers.py 不得 import os"

    source = (SRC / "mappers.py").read_text(encoding="utf-8")
    assert "os.environ" not in source
    assert "getenv" not in source

    # 有**两个** `to_native`：`SubmitPayloadMapper` 里的协议声明（函数体是 `...`）
    # 与 `FakeChannel` 的实现。挑错一个就会得到"没有 model 赋值"这种假绿 ——
    # 所以按类名挑，并断言挑到的是有函数体的那个。
    to_native = None
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "FakeChannel":
            to_native = next(
                (n for n in cls.body
                 if isinstance(n, ast.FunctionDef) and n.name == "to_native"),
                None)
            break
    assert to_native is not None and len(to_native.body) > 1, \
        "没找到 FakeChannel.to_native 的实现体"
    # `to_native` 把请求写成 dict 字面量（`{"model": ...}`），所以键是
    # `ast.Constant` 而不是下标或 keyword。三种写法都认，否则这条断言在最该
    # 看见它的地方看不见东西 —— 那就成了一条永远绿的假断言。
    model_sources = []
    for node in ast.walk(to_native):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Subscript) and \
                        isinstance(target.slice, ast.Constant) and \
                        target.slice.value == "model":
                    model_sources.append(ast.unparse(node.value))
        if isinstance(node, ast.keyword) and node.arg == "model":
            model_sources.append(ast.unparse(node.value))
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "model":
                    model_sources.append(ast.unparse(value))
    assert model_sources, "断言本身要能看见 model 的赋值"
    assert all("payload" in s for s in model_sources), model_sources


def test_t27_26_all_connector_yaml_are_disabled_and_unprobed():
    """T27-26 + AGENTS.md「不预填供应商状态为 active」。"""
    import yaml

    files = sorted((ROOT / "connectors").glob("*/*/connector.yaml"))
    assert len(files) >= 9, f"只找到 {len(files)} 个渠道定义"
    for path in files:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert doc["status"] != "active", f"{path} 预填成 active"
        assert doc["lifecycle"] != "active", f"{path} 预填成 active"
        assert doc["evidence_checked_at"] is None, f"{path} 假装已探针"
        assert doc["may_change_model"] is False
        assert doc["may_adjust_budget"] is False
        assert doc["may_accept_output"] is False


def test_t27_27_unregistered_channel_is_never_acked():
    """T27-27：`sora/historical` 不部署 → 没有适配器 → 不 ACK、不发事件。"""
    h = Harness()
    env = h.submit_env(channel="sora/historical")

    with pytest.raises(UnknownChannel):
        h.svc.consume(env)

    assert h.calls == []
    assert h.events() == []
    assert h.count("connector_command_inbox") == 0, \
        "不认识的渠道不能留下已处理的痕迹"


def test_t27_28_channel_isolation():
    """T27-28：一个渠道熔断不影响另一个渠道。"""
    kling = FakeChannel(channel_route="kling/broken")
    veo = FakeChannel(channel_route="veo/official")

    def transport(op, req):
        if req["channel_route"] == "kling/broken":
            raise TransportError("connect")
        return TransportResult(200, {"id": "task_veo"})

    h = Harness(channels=[kling, veo], transport=transport)

    for i in range(6):
        h.svc.consume(h.submit_env(f"dsp_k{i}", channel="kling/broken"))
    assert h.svc.health("kling/broken")["breaker_state"] == "open"

    result = h.svc.consume(h.submit_env("dsp_v1", channel="veo/official"))
    assert result["outcome"] == "accepted"
    assert h.svc.health("veo/official")["breaker_state"] == "closed"


def test_t27_29_no_prompt_text_leaves_in_events_or_receipts():
    """T27-29：日志/事件脱敏。断言 prompt 原文不进观测也不进回执。"""
    prompt = "SECRET-PROMPT-9f3a"
    h = Harness()
    h.svc.consume(h.submit_env("dsp_1", payload={
        "mode": "text_to_video", "duration_seconds": 5,
        "model_id": "fake", "prompt": {"text": prompt}}))

    for event in h.events():
        assert prompt not in json.dumps(event, ensure_ascii=False)
    for receipt in h.receipts():
        assert prompt not in json.dumps(receipt, ensure_ascii=False,
                                        default=str)
    # 但请求本身确实带了它（否则这条断言是空的）。
    assert prompt in json.dumps(h.calls[0][1], ensure_ascii=False)


def test_t27_30_per_channel_secrets_are_independent():
    """T27-30：删掉一个渠道的 secret 不影响另一个。"""
    a = FakeChannel(channel_route="a/ch")
    b = FakeChannel(channel_route="b/ch")
    h = Harness(channels=[a, b],
                secrets={"a/ch": b"secret-a", "b/ch": b"secret-b"})

    ts = "1762900000"
    body = callback_body()
    # a 的 secret 被删了
    del h._secrets["a/ch"]
    with pytest.raises(ServiceError) as exc:
        h.svc.receive_callback("a/ch", body,
                               {"x-timestamp": ts,
                                "x-signature": signed(body, ts, b"secret-a")},
                               now_epoch=float(ts))
    assert exc.value.code == "DEPENDENCY_UNAVAILABLE"

    # b 照常验签通过
    ok = h.svc.receive_callback(
        "b/ch", body,
        {"x-timestamp": ts, "x-signature": signed(body, ts, b"secret-b")},
        now_epoch=float(ts))
    assert ok["accepted"] is True


# ================================================================ 观测形状

def test_observation_has_exactly_the_twelve_contract_keys():
    """§6.5 + `additionalProperties: false`：12 个键，一个不多一个不少。"""
    h = Harness()
    h.svc.consume(h.submit_env())
    data = h.events()[0]["data"]

    expected = {"schema_version", "channel_route", "dispatch_id",
                "stable_client_key", "provider_job_ref", "source",
                "observed_at", "provider_status_raw", "status", "terminal",
                "output", "provider_usage"}
    assert set(data) == expected


def test_submit_outcome_carries_the_marker_s26_dispatches_on():
    """C27-6：S26 按 `status ∈ SUBMIT_OUTCOMES` + `provider_status_raw ==
    "SUBMIT_OUTCOME"` 分派到 `record_submit_outcome`。两者缺一不可。"""
    from provider_connectors.service import SUBMIT_OUTCOME_MARKER

    h = Harness()
    h.svc.consume(h.submit_env())
    data = h.events()[0]["data"]

    assert data["status"] == "accepted"
    assert data["provider_status_raw"] == SUBMIT_OUTCOME_MARKER
    assert data["terminal"] is False


def test_provider_usage_sits_beside_output_not_inside():
    """C27-9：`from_native_status` 把 usage 提到平级。"""
    ch = FakeChannel()
    obs = ch.from_native_status({
        "id": "task_1", "state": "SUCCEEDED",
        "output": {"url": "https://x/y.mp4"},
        "usage": {"generated_seconds": 5.0}})

    assert obs["provider_usage"] == {"generated_seconds": 5.0}
    assert "usage" not in (obs["output"] or {})


def test_unknown_provider_status_is_inconclusive_not_guessed():
    """§4 规则 3：没见过的状态值报 `inconclusive`，不猜一个六值。"""
    ch = FakeChannel()
    obs = ch.from_native_status({"id": "task_1", "state": "WOBBLING"})
    assert obs["status"] == "inconclusive"
    assert obs["provider_status_raw"] == "WOBBLING"
    assert obs["terminal"] is False


def test_not_found_is_not_translated_to_failed():
    """§3.5：`not_found` 不能翻译成 `failed`（S26 对两者的处置不同）。"""
    ch = FakeChannel()
    obs = ch.from_native_status({"id": "task_1", "state": "NOT_FOUND"})
    assert obs["status"] == "not_found"
    assert obs["terminal"] is False


def test_status_command_without_ref_applies_nothing():
    """没有 `provider_job_ref` 就没东西可查 —— 不假装查过、也不记渠道故障。"""
    h = Harness()
    h.svc.consume(h.submit_env())
    env = h.status_env(provider_job_ref=None)
    env["data"]["provider_job_ref"] = None

    result = h.svc.consume(env)
    assert result["applied"] is False
    assert result["reason"] == "NO_PROVIDER_JOB_REF"
    assert h.svc.health(CHANNEL)["last_error_class"] is None


def test_status_5xx_reports_inconclusive_and_counts_toward_breaker():
    """§3.5 / §5.3：查不动 → `inconclusive`，且这**是**渠道故障。"""
    h = Harness(transport=lambda op, req: TransportResult(503, {}))
    h.svc.consume(h.submit_env("dsp_1"))
    result = h.svc.consume(h.status_env(dispatch_id="dsp_1"))

    assert result["status"] == "inconclusive"
    assert h.svc.health(CHANNEL)["last_error_class"] == "provider_5xx"


def test_cancel_unsupported_reports_without_calling_out():
    """§3.6：`cancel_semantics=unsupported` → 不发请求，只报事实。"""
    ch = FakeChannel(channel_route="fake/no-cancel",
                     cancel_semantics="unsupported")
    h = Harness(channels=[ch], secrets={"fake/no-cancel": SECRET})
    env = {"id": "evt_c", "type": "connector.cancel.requested",
           "tenant_id": TENANT, "aggregate_id": "pj_1", "correlation_id": "c",
           "data": {"channel_route": "fake/no-cancel",
                    "provider_job_id": "pj_1", "provider_job_ref": "task_A",
                    "dispatch_id": "dsp_1", "stable_client_key": "dr_dsp_1"}}
    result = h.svc.consume(env)

    assert result["applied"] is True
    assert result["status"] == "inconclusive"
    assert h.calls == []
    assert h.events()[-1]["data"]["provider_status_raw"] == "CANCEL_UNSUPPORTED"


# ================================================================ 事件面

def test_unknown_event_type_is_refused():
    h = Harness()
    with pytest.raises(ServiceError) as exc:
        h.svc.consume({"id": "e", "type": "connector.teleport.requested",
                       "tenant_id": TENANT, "aggregate_id": "x",
                       "data": {"channel_route": CHANNEL}})
    assert exc.value.code == "UNSUPPORTED_EVENT"


def test_idempotency_class_mismatch_is_refused_before_any_bytes():
    """§5.4：S26 记的档位与适配器实测不符 → 两侧对"重发是否安全"答案不同。"""
    h = Harness()
    env = h.submit_env(provider_idempotency="supported")
    with pytest.raises(ServiceError) as exc:
        h.svc.consume(env)

    assert exc.value.code == "IDEMPOTENCY_CLASS_MISMATCH"
    assert h.calls == []
    assert h.claims == []


def test_unsupported_idempotency_writes_local_index():
    """§5.4：`unsupported` 档必须写 `connector_client_key_index`。"""
    ch = FakeChannel(channel_route="fake/no-idem",
                     idempotency_class="unsupported")
    h = Harness(channels=[ch], secrets={"fake/no-idem": SECRET})
    env = h.submit_env("dsp_1", channel="fake/no-idem",
                       provider_idempotency="unsupported")
    h.svc.consume(env)

    row = h.conn.execute("SELECT * FROM connector_client_key_index").fetchone()
    assert row["stable_client_key"] == "dr_dsp_1"
    assert row["provider_job_ref"] == "task_A"


def test_receipts_are_scoped_by_tenant():
    """跨租户读回执 → 404，不是别人的数据。"""
    h = Harness()
    h.svc.consume(h.submit_env())
    receipt_id = h.receipts()[0]["receipt_id"]

    with pytest.raises(ServiceError) as exc:
        h.svc.get_receipt("t_other", receipt_id)
    assert exc.value.status == 404
    assert h.svc.get_receipt(TENANT, receipt_id)["dispatch_id"] == "dsp_1"
