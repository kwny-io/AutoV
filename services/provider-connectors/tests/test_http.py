"""S27 的 HTTP 面。断言的是**入口约束**，不是重复 service 的语义测试。

重点只有三件：

1. `submit` 没有 HTTP 入口 —— 付费路径必须由事件驱动，否则就多出一条能绕过
   claim 的门（§5.2）。
2. `/callbacks/{route}` **不带租户头**：供应商不认识平台的租户模型，它唯一的
   身份来源是 HMAC。给它加一个 `X-Tenant-Id` 要求会让真实回调全部 400。
3. 租户隔离与内部 principal 校验，同 S26 的形状。
"""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from provider_connectors import FakeChannel, TransportResult, initialise

import importlib.util
from pathlib import Path

APP_PATH = Path(__file__).resolve().parents[1] / "api" / "app.py"


def _load_app_module():
    """按文件加载，避免 `provider_connectors.api` 需要 `__init__.py` 才能导入。"""
    spec = importlib.util.spec_from_file_location("s27_api_under_test", APP_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SECRET = b"secret-alpha"
TENANT = "t_alpha"
H = {"X-Tenant-Id": TENANT, "X-Principal": "S26"}


def _grant(tenant_id, dispatch_id, body):
    return {"provider_job_id": "pj_" + dispatch_id,
            "stable_client_key": "dr_" + dispatch_id,
            "fencing_token": body["fencing_token"],
            "deadline": "2026-11-12T00:00:00.000000Z",
            "request_hash": "rh"}


@pytest.fixture()
def client():
    module = _load_app_module()
    conn = sqlite3.connect(":memory:", check_same_thread=False,
                           isolation_level=None)
    initialise(conn)
    app = module.create_app(
        conn=conn, channels=[FakeChannel()],
        transport=lambda op, req: TransportResult(200, {"id": "task_A"}),
        claim_grant=_grant,
        secret_for=lambda route: SECRET)
    return TestClient(app)


def _event(dispatch_id="dsp_1"):
    return {"id": "evt_" + dispatch_id,
            "type": "connector.submit.requested",
            "tenant_id": TENANT, "aggregate_id": dispatch_id,
            "correlation_id": "corr",
            "data": {"channel_route": "fake/channel",
                     "dispatch_id": dispatch_id,
                     "provider_job_id": "pj_" + dispatch_id,
                     "stable_client_key": "dr_" + dispatch_id,
                     "fencing_token": 3, "request_hash": "rh",
                     "provider_idempotency": "client_lookup_only",
                     "tenant_id": TENANT,
                     "submit_payload_inline": {"mode": "text_to_video",
                                               "duration_seconds": 5}}}


def test_submit_has_no_http_entry(client):
    """付费路径只能由事件驱动：`connector-commands` 明确拒绝 `submit`。"""
    response = client.post(
        "/connector-commands",
        json={"op": "submit", "channel_route": "fake/channel"},
        headers={**H, "Idempotency-Key": "k-submit"})

    assert response.status_code == 400
    assert response.json()["code"] == "UNSUPPORTED_OPERATION"


def test_connector_commands_serves_the_three_side_effect_free_ops(client):
    caps = client.post("/connector-commands",
                       json={"op": "capabilities",
                             "channel_route": "fake/channel"},
                       headers={**H, "Idempotency-Key": "k-caps"})
    assert caps.status_code == 202
    assert caps.json()["channel_route"] == "fake/channel"

    # 同 key 同 body 原样重放（不是重新计算）。
    again = client.post("/connector-commands",
                        json={"op": "capabilities",
                              "channel_route": "fake/channel"},
                        headers={**H, "Idempotency-Key": "k-caps"})
    assert again.json() == caps.json()


def test_receipt_query_is_tenant_scoped(client):
    created = client.post("/internal/events/connector.submit.requested",
                          json=_event(), headers=H)
    receipt_id = created.json()["receipt_id"]

    assert client.get(f"/connector-receipts/{receipt_id}",
                      headers=H).json()["outcome"] == "accepted"
    assert client.get(f"/connector-receipts/{receipt_id}",
                      headers={"X-Tenant-Id": "t_other"}).status_code == 404
    assert client.get(f"/connector-receipts/{receipt_id}").status_code == 400


def test_internal_events_rejects_foreign_tenant_and_principal(client):
    body = _event("dsp_tenant")

    assert client.post("/internal/events/connector.submit.requested",
                       json={**body, "tenant_id": "t_other"},
                       headers=H).status_code == 403
    assert client.post("/internal/events/connector.submit.requested",
                       json=body,
                       headers={"X-Tenant-Id": TENANT,
                                "X-Principal": "nobody"}).status_code == 403


def test_internal_events_rejects_topic_mismatch(client):
    body = _event("dsp_topic")
    body["type"] = "connector.status.requested"

    response = client.post("/internal/events/connector.submit.requested",
                           json=body, headers=H)
    assert response.status_code == 400
    assert response.json()["code"] == "MALFORMED_REQUEST"


def test_callback_needs_no_tenant_header(client):
    """回调的身份只来自 HMAC —— 加租户头要求会让真实供应商全部 400。"""
    # 走真实的 300s 时间窗，所以时间戳必须是"现在"。写死一个常数会在
    # 窗口外被拒，看起来像签名失败，其实与签名无关。
    ts = str(int(time.time()))
    body = b'{"id":"task_A","state":"SUCCEEDED","client_key":"dr_dsp_1"}'
    sig = hmac.new(SECRET, ts.encode() + b"." + body,
                   hashlib.sha256).hexdigest()

    response = client.post("/callbacks/fake/channel", content=body,
                           headers={"x-timestamp": ts, "x-signature": sig})

    assert response.status_code == 200
    assert response.json()["accepted"] is True


def test_callback_without_valid_signature_is_401_and_not_stored(client):
    response = client.post("/callbacks/fake/channel", content=b"{}",
                           headers={"x-timestamp": str(int(time.time())),
                                    "x-signature": "00" * 32})
    assert response.status_code == 401
    assert response.json()["code"] == "CALLBACK_SIGNATURE_INVALID"


def test_janitor_reports_flat_counts(client):
    result = client.post("/janitor:sweep",
                         headers={"X-Principal": "janitor"}).json()
    assert result == {"recovered": 0, "receipt_ids": [], "mapped": 0,
                      "unmapped": 0, "dead_letter": 0}
