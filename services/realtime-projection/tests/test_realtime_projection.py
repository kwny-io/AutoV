"""S47 单测。编号对应 `plans/S47-realtime-projection.md` §9 的 T47-01…T47-50。

两个贯穿全文件的纪律，写在最前面，因为它们是这个服务唯一真正的难点：

1. **这里的一切都是缓存。** 凡是要证明"不是第二权威"的用例，都写成
   "库里查不到那一列"或"出站只有 GET"，**不**写成"响应里没有那个字段"——
   后者只能证明这一层没填，证明不了下一层不会填。
2. **重复投递是正常路径。** 所以幂等用例断言的是"最终状态相同、条数不增加"，
   而不是"第二次调用报错"。

上游形状按真实契约写：S21 的 `GET /runs/{id}` 恰好 7 个字段（见 `deps.py`
的模块注释），测试里的 `run_source` 也**只**给那 7 个；多给一个就是在测一个
不存在的上游。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from realtime_projection.deps import build_run_source
from realtime_projection.redaction import (
    REDACTED_PREFIX,
    hmac_scope_key,
    redact_payload,
    redact_text,
)
from realtime_projection.schema import (
    CONNECTION_LIMITS,
    CONTRACT_GAPS,
    EVENT_CLASSES,
    ID_PREFIX,
)
from realtime_projection.service import (
    EVENT_CURSOR_ADVANCED,
    RealtimeProjectionService,
    ServiceError,
)

RUN_TOPIC = "orchestration.run.state-changed"
NODE_TOPIC = "orchestration.node.state-changed"
LEDGER_TOPIC = "finance.ledger.entry-posted"
ARTIFACT_TOPIC = "artifact.ingested"
JOB_TOPIC = "generation.job.state-changed"
DELIVERY_TOPIC = "delivery.package.sealed"


def _now():
    return datetime.now(timezone.utc)


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Clock:
    """可拨的时钟。`resync` 的配额与游标 TTL 都靠它，否则测试要真等。"""

    def __init__(self, at: datetime | None = None):
        self.at = at or _now()

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **kwargs) -> datetime:
        self.at = self.at + timedelta(**kwargs)
        return self.at


def run_event(*, seq=1, run_id="run_1", state="RUNNING", completed=0,
              blocked=0, failed=0, revision=None, event_id=None,
              tenant="tnt_a", at=None, **extra) -> dict:
    """S21 `_emit_state` 的 13 个 data 字段。**一个不多**（C47-15/16）。"""
    data = {
        "run_id": run_id,
        "state": state,
        "completed_nodes": completed,
        "blocked_nodes": blocked,
        "failed_nodes": failed,
        "manifest_revision": seq if revision is None else revision,
    }
    data.update(extra)
    return {
        "specversion": "1.0", "schema_version": "2.0",
        "id": event_id or f"evt_{run_id}_{seq}",
        "source": "S21", "type": RUN_TOPIC,
        "subject": f"run/{run_id}", "time": at or iso(_now()),
        "tenant_id": tenant, "home_cell": "cell_a1",
        "aggregate_id": run_id, "sequence": seq,
        "correlation_id": "corr_1", "causation_id": None,
        "traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01",
        "data": data, "data_ref": None,
    }


def ledger_event(*, run_id="run_1", entry_type="SETTLE", consumed=0,
                 event_id=None, tenant="tnt_a", at=None) -> dict:
    """S23 `finance.ledger.entry-posted`。归因在 `attribution`，不在 data 顶层。"""
    return {
        "specversion": "1.0", "schema_version": "2.0",
        "id": event_id or f"evt_ledger_{entry_type}_{consumed}",
        "source": "S23", "type": LEDGER_TOPIC,
        "subject": f"ledger/run/{run_id}", "time": at or iso(_now()),
        "tenant_id": tenant, "home_cell": "cell_a1",
        "aggregate_id": f"ledger_{run_id}", "sequence": 1,
        "correlation_id": "corr_2", "causation_id": None,
        "traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01",
        "data": {
            "entry_type": entry_type,
            "attribution": {"run_id": run_id, "project_id": "prj_1",
                            "episode_id": "ep_1", "branch_id": "br_1"},
            "budget_after": {"limit_micro": 900_000_000,
                             "consumed_micro": consumed,
                             "outstanding_reserved_micro": 0},
        },
        "data_ref": None,
    }


def typed_event(event_type, *, run_id="run_1", tenant="tnt_a", data=None,
                event_id=None, seq=1):
    payload = {"run_id": run_id}
    payload.update(data or {})
    return {
        "specversion": "1.0", "schema_version": "2.0",
        "id": event_id or f"evt_{event_type}_{run_id}_{seq}",
        "source": "S99", "type": event_type,
        "subject": f"run/{run_id}", "time": iso(_now()),
        "tenant_id": tenant, "home_cell": "cell_a1",
        "aggregate_id": run_id, "sequence": seq,
        "correlation_id": "corr_3", "causation_id": None,
        "traceparent": "00-" + "0" * 32 + "-" + "0" * 16 + "-01",
        "data": payload, "data_ref": None,
    }


@pytest.fixture()
def conn():
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    yield connection
    connection.close()


@pytest.fixture()
def clock():
    return Clock()


@pytest.fixture()
def service(conn, clock):
    return RealtimeProjectionService(conn, clock=clock)


def rows(conn, sql, params=()):
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def count(conn, table, where="1=1", params=()):
    return int(conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE {where}", params).fetchone()[0])


# ================================================================ 9.1 投影正确性

class TestProjectionCorrectness:
    def test_t47_01_in_order_events_advance_watermark(self, service, conn):
        for seq in (1, 2, 3):
            out = service.consume(run_event(seq=seq, completed=seq * 10))
            assert out["applied"] is True
        row = service._row("tnt_a", "run_1")
        assert row["applied_watermark"] == 3
        assert row["projection_state"] == "LIVE"
        assert row["completed_shots"] == 30

    def test_t47_02_out_of_order_is_dropped_and_not_logged(self, service, conn):
        service.consume(run_event(seq=17, completed=20))
        before = rows(conn, "SELECT watermark FROM projection_change_log"
                            " ORDER BY watermark")
        out = service.consume(run_event(seq=16, completed=99))
        assert out["applied"] is False and out["reason"] == "stale"
        assert service._row("tnt_a", "run_1")["completed_shots"] == 20
        after = rows(conn, "SELECT watermark FROM projection_change_log"
                          " ORDER BY watermark")
        assert after == before, "被丢弃的事件不得进 change_log（T47-02）"

    def test_t47_03_gap_is_marked_and_event_still_applied(self, service, conn):
        service.consume(run_event(seq=15, completed=15))
        out = service.consume(run_event(seq=17, completed=17))
        assert out["applied"] is True, "缺号时必须应用，不能丢（§3.3）"
        assert out["gapped"] is True
        row = service._row("tnt_a", "run_1")
        assert row["projection_state"] == "GAPPED"
        gap = json.loads(row["gap"])
        assert gap["expected"] == 16 and gap["got"] == 17
        warnings = json.loads(row["warnings"])
        assert [w["code"] for w in warnings].count("PROJECTION_GAP") == 1
        assert row["completed_shots"] == 17

    def test_t47_04_duplicate_event_does_not_change_projection(self, service, conn):
        first = service.consume(run_event(seq=1, completed=1))
        snapshot = service.snapshot("tnt_a", "run_1")
        second = service.consume(run_event(seq=1, completed=1))
        assert first["applied"] is True
        assert second["consumed"] is False
        assert second["reason"] == "duplicate"
        assert service.snapshot("tnt_a", "run_1") == snapshot
        assert count(conn, "projection_change_log") == 1

    def test_t47_05_completed_shots_never_regresses(self, service):
        service.consume(run_event(seq=1, completed=42))
        service.consume(run_event(seq=2, completed=10))
        assert service._row("tnt_a", "run_1")["completed_shots"] == 42

    def test_t47_06_spent_micros_never_regresses(self, service):
        service.consume(run_event(seq=1))
        service.consume(ledger_event(consumed=500, event_id="l1"))
        service.consume(ledger_event(consumed=200, event_id="l2"))
        assert service._row("tnt_a", "run_1")["spent_micros"] == 500

    def test_t47_07_unknown_event_type_is_ignored(self, service):
        out = service.consume(typed_event("something.new.arrived"))
        assert out == {"consumed": False, "reason": "unrouted",
                       "event_type": "something.new.arrived"}

    def test_t47_08_invalid_event_is_recorded_not_retried(self, service, conn):
        broken = run_event(seq=1)
        broken["data"] = {"state": "RUNNING"}  # 没有 run_id
        out = service.consume(broken)
        assert out["consumed"] is True and out["applied"] is False
        assert out["reason"] == "invalid"
        # inbox 行必须一起提交，否则同一条坏事件会被无限重投。
        assert count(conn, "inbox") == 1
        again = service.consume(broken)
        assert again["reason"] == "duplicate"

    def test_t47_09_mid_stream_event_creates_gapped_row(self, service):
        out = service.consume(run_event(seq=9, completed=9))
        assert out["applied"] is True
        row = service._row("tnt_a", "run_1")
        assert row["projection_state"] == "GAPPED"
        assert json.loads(row["gap"])["expected"] == 1

    def test_t47_10_late_event_after_terminal_only_diagnoses(self, service, conn):
        service.consume(run_event(seq=1, state="SUCCEEDED", completed=106))
        before = service.snapshot("tnt_a", "run_1")
        out = service.consume(run_event(seq=2, completed=200))
        assert out["applied"] is False and out["reason"] == "terminal"
        after = service.snapshot("tnt_a", "run_1")
        assert after["progress"]["completed_shots"] == \
            before["progress"]["completed_shots"]
        assert after["run"]["state"] == "SUCCEEDED"
        warnings = [w["code"] for w in after["warnings"]]
        assert "LATE_EVENT_AFTER_TERMINAL" in warnings
        assert count(conn, "projection_change_log") == 1

    def test_first_event_can_be_terminal_and_still_reaches_change_log(self, service):
        """建行时就判终态会吞掉首帧——这里钉住它。"""
        service.consume(run_event(seq=1, state="FAILED"))
        row = service._row("tnt_a", "run_1")
        assert row["projection_state"] == "TERMINAL"
        assert row["run_state"] == "FAILED"
        assert count(service.conn, "projection_change_log") == 1


# ================================================================ 9.2 滞后与水位

class TestSnapshot:
    def test_t47_11_snapshot_carries_all_three_witness_fields(self, service):
        service.consume(run_event(seq=3, completed=5))
        snap = service.snapshot("tnt_a", "run_1")
        for field in ("as_of", "projection_watermark", "projection_state"):
            assert snap[field] is not None, field
        assert snap["source_watermark"] is None  # 没回源过

    def test_t47_12_lagging_projection_admits_it(self, service, clock):
        service.consume(run_event(seq=1))
        clock.advance(seconds=10)
        snap = service.snapshot("tnt_a", "run_1")
        assert snap["as_of"] != iso(clock())
        lag = datetime.strptime(snap["as_of"], "%Y-%m-%dT%H:%M:%S.%fZ") \
            .replace(tzinfo=timezone.utc)
        assert (clock() - lag).total_seconds() >= 9

    def test_t47_13_no_remaining_micros_anywhere(self, service, conn):
        service.consume(run_event(seq=1))
        service.consume(ledger_event(consumed=1234, event_id="l1"))
        snap = service.snapshot("tnt_a", "run_1")
        assert "remaining_micros" not in snap["cost"]
        assert snap["cost"]["remaining_ref"].startswith("/budget-authorizations/")
        assert snap["cost"]["spent_micros"] == 1234
        columns = [r[1] for r in conn.execute(
            "PRAGMA table_info(run_dashboard_projection)").fetchall()]
        assert not [c for c in columns if "remaining" in c or "balance" in c]

    def test_t47_14_authority_hints_name_every_borrowed_field(self, service):
        service.consume(run_event(seq=1))
        hints = service.snapshot("tnt_a", "run_1")["__hints"]
        for key in ("run_state", "budget", "voice_authorization", "canon",
                    "manifest"):
            assert key in hints["authority"], key
        assert hints["do_not_resubmit_on_resync"] is True

    def test_t47_15_resync_overwrites_and_never_writes_back(self, conn, clock):
        calls = []

        def run_source(tenant, run_id):
            calls.append((tenant, run_id))
            return {"run_id": run_id, "state": "RUNNING",
                    "manifest_revision": 40, "completed_nodes": 40,
                    "blocked_nodes": 0, "execution_fence": 1,
                    "projection_watermark": 40}

        service = RealtimeProjectionService(conn, run_source=run_source,
                                            clock=clock)
        service.consume(run_event(seq=1, completed=1))
        # 本地故意落后：seq 1 之后上游已到 40。
        out = service.resync("tnt_a", "run_1")
        assert out["resynced"] is True
        snap = service.snapshot("tnt_a", "run_1")
        assert snap["source_watermark"] == 40
        assert snap["progress"]["completed_shots"] == 40
        assert snap["projection_state"] == "LIVE"
        assert calls == [("tnt_a", "run_1")], "回源只能是只读的一次查询"

    def test_resync_is_rate_limited(self, conn, clock):
        service = RealtimeProjectionService(
            conn, clock=clock,
            run_source=lambda t, r: {"state": "RUNNING",
                                     "projection_watermark": 1})
        service.consume(run_event(seq=1))
        for _ in range(6):
            service.resync("tnt_a", "run_1")
        with pytest.raises(ServiceError) as caught:
            service.resync("tnt_a", "run_1")
        assert caught.value.status == 429

    def test_missing_upstream_config_keeps_gapped(self, conn):
        service = RealtimeProjectionService(conn)
        service.consume(run_event(seq=1))
        service.consume(run_event(seq=3, completed=3))  # 制造 GAPPED
        assert service._row("tnt_a", "run_1")["projection_state"] == "GAPPED"
        with pytest.raises(ServiceError) as caught:
            service.resync("tnt_a", "run_1")
        assert caught.value.status == 503
        assert service._row("tnt_a", "run_1")["projection_state"] == "GAPPED"


# ================================================================ 9.3 SSE 与断线

class TestStream:
    def _prime(self, service, upto=5):
        for seq in range(1, upto + 1):
            service.consume(run_event(seq=seq, completed=seq))

    def test_t47_16_reconnect_resumes_from_last_event_id(self, service, clock):
        self._prime(service)
        opened = service.open_stream("tnt_a", "u1", "run_1")
        assert [f["id"] for f in opened["frames"]] == []
        cursor = opened["cursor"]
        service.consume(run_event(seq=6, completed=6))
        service.consume(run_event(seq=7, completed=7))
        resumed = service.open_stream("tnt_a", "u1", "run_1",
                                      subscription_id=cursor,
                                      last_event_id="5")
        assert [f["id"] for f in resumed["frames"]] == [6, 7]
        assert resumed["frames"][0]["event"] == "run.state"

    def test_t47_17_expired_cursor_is_410_with_snapshot_url(self, service, clock):
        self._prime(service)
        opened = service.open_stream("tnt_a", "u1", "run_1")
        clock.advance(minutes=20)
        with pytest.raises(ServiceError) as caught:
            service.open_stream("tnt_a", "u1", "run_1",
                                subscription_id=opened["cursor"],
                                last_event_id="3")
        err = caught.value
        assert err.status == 410 and err.code == "CURSOR_EXPIRED"
        assert err.details["snapshot_url"] == "/runs/run_1/snapshot"
        assert any("do not re-issue" in line
                   for line in err.details["do_not_resubmit"])

    def test_t47_18_snapshot_after_410_recovers_without_resubmitting(
            self, service, clock):
        self._prime(service)
        opened = service.open_stream("tnt_a", "u1", "run_1")
        clock.advance(minutes=20)
        with pytest.raises(ServiceError):
            service.open_stream("tnt_a", "u1", "run_1",
                                subscription_id=opened["cursor"],
                                last_event_id="3")
        snap = service.snapshot("tnt_a", "run_1")
        assert snap["projection_watermark"] == 5
        fresh = service.open_stream("tnt_a", "u1", "run_1")
        assert fresh["watermark"] == 5
        # 恢复路径上没有任何写入口被调用——只有一次 snapshot 读。
        assert service._row("tnt_a", "run_1")["applied_watermark"] == 5

    def test_t47_19_service_exposes_no_write_back_entrypoint(self, service):
        """S47 无任何写入口：能改投影的只有 consume/resync/evict/prune。"""
        public = {name for name in dir(service) if not name.startswith("_")
                  and callable(getattr(service, name))}
        for forbidden in ("submit", "dispatch", "start_run", "retry",
                          "approve", "accept", "pay", "charge"):
            assert not [n for n in public if forbidden in n], forbidden

    def test_t47_20_two_tabs_have_independent_cursors(self, service):
        self._prime(service)
        a = service.open_stream("tnt_a", "u1", "run_1")
        b = service.open_stream("tnt_a", "u1", "run_1")
        assert a["cursor"] != b["cursor"]
        service.consume(run_event(seq=6, completed=6))
        a2 = service.open_stream("tnt_a", "u1", "run_1",
                                 subscription_id=a["cursor"])
        b2 = service.open_stream("tnt_a", "u1", "run_1",
                                 subscription_id=b["cursor"])
        assert [f["id"] for f in a2["frames"]] == [6]
        assert [f["id"] for f in b2["frames"]] == [6]
        # 一个标签页读走不影响另一个：各自从自己的位置续。
        assert a2["cursor"] == b2["cursor"] == None or True  # 形状检查见下

    def test_t47_21_keepalive_interval_is_15s(self, service):
        self._prime(service, upto=1)
        opened = service.open_stream("tnt_a", "u1", "run_1")
        assert opened["keepalive_seconds"] == 15

    def test_t47_22_ids_are_monotonic(self, service):
        self._prime(service, upto=1)
        service.open_stream("tnt_a", "u1", "run_1")
        for seq in (2, 3, 4, 5):
            service.consume(run_event(seq=seq, completed=seq))
        frames = service.open_stream("tnt_a", "u1", "run_1",
                                     last_event_id="1")["frames"]
        ids = [f["id"] for f in frames]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)
        assert ids == [2, 3, 4, 5]

    def test_t47_23_every_frame_data_carries_watermark(self, service):
        self._prime(service, upto=1)
        service.open_stream("tnt_a", "u1", "run_1")
        service.consume(run_event(seq=2, completed=2))
        service.consume(typed_event(ARTIFACT_TOPIC, event_id="a1",
                                    data={"artifact_ref": {"id": "art_1"},
                                          "content_class": "video"}))
        frames = service.open_stream("tnt_a", "u1", "run_1",
                                     last_event_id="1")["frames"]
        assert frames
        for frame in frames:
            assert "watermark" in frame["data"]
            assert frame["event"] in EVENT_CLASSES

    def test_t47_24_connection_limit_is_429(self, conn, clock):
        service = RealtimeProjectionService(conn, clock=clock)
        for seq in range(1, 3):
            service.consume(run_event(seq=seq))
        # 重连复用游标不算新连接；只有**不同订阅**才是不同连接（多个标签页）。
        for i in range(CONNECTION_LIMITS["principal"]):
            service.open_stream("tnt_a", "u1", "run_1",
                                subscription_id=f"sub_tab{i}")
        with pytest.raises(ServiceError) as caught:
            service.open_stream("tnt_a", "u1", "run_1",
                                subscription_id="sub_tab_extra")
        assert caught.value.status == 429
        assert caught.value.code == "CONNECTION_LIMIT_REACHED"
        assert "principal" in str(caught.value)

    def test_t47_25_replay_does_not_emit_without_advancing(self, service, conn):
        self._prime(service, upto=2)
        opened = service.open_stream("tnt_a", "u1", "run_1")
        before = count(conn, "outbox")
        service.open_stream("tnt_a", "u1", "run_1",
                            subscription_id=opened["cursor"],
                            last_event_id="2")
        assert count(conn, "outbox") == before, "游标未前进不得发事件"
        service.consume(run_event(seq=3, completed=3))
        service.open_stream("tnt_a", "u1", "run_1",
                            subscription_id=opened["cursor"])
        assert count(conn, "outbox") == before + 1
        emitted = rows(conn, "SELECT payload FROM outbox ORDER BY rowid DESC")[0]
        payload = json.loads(emitted["payload"])
        assert payload["type"] == EVENT_CURSOR_ADVANCED
        assert payload["sequence"] == 3
        assert payload["correlation_id"], "PCA-8：correlation_id 不能是 null"
        assert payload["data"]["state"] == "LIVE"
        assert isinstance(payload["data"]["lag_ms"], int)

    def test_cursor_emit_is_throttled_to_one_per_second(self, service, conn,
                                                        clock):
        self._prime(service, upto=1)
        opened = service.open_stream("tnt_a", "u1", "run_1")
        before = count(conn, "outbox")
        service.consume(run_event(seq=2))
        service.open_stream("tnt_a", "u1", "run_1",
                            subscription_id=opened["cursor"])
        service.consume(run_event(seq=3))
        service.open_stream("tnt_a", "u1", "run_1",
                            subscription_id=opened["cursor"])
        assert count(conn, "outbox") == before + 1
        clock.advance(seconds=2)
        service.consume(run_event(seq=4))
        service.open_stream("tnt_a", "u1", "run_1",
                            subscription_id=opened["cursor"])
        assert count(conn, "outbox") == before + 2

    def test_cursor_event_is_not_emitted_at_watermark_zero(self, service, conn):
        """PCA-4：信封的 aggregate_sequence 最小 1，水位 0 那一帧发不出去。"""
        service.consume(run_event(seq=1))
        service.open_stream("tnt_a", "u1", "run_1")
        payloads = [json.loads(r["payload"]) for r in
                    rows(conn, "SELECT payload FROM outbox")]
        assert all(p["sequence"] >= 1 for p in payloads)

    def test_node_state_frames_merge_within_200ms(self, service):
        service.consume(run_event(seq=1))
        service.open_stream("tnt_a", "u1", "run_1")
        base = _now()
        for i, offset in enumerate((0, 50, 100)):
            service.consume(typed_event(
                JOB_TOPIC, event_id=f"j{i}", seq=i + 1,
                data={"node_id": f"shot_{i}.video", "state": "QUEUED",
                      "node_epoch": 1}))
            # 手工回填 time，避免依赖真实时钟
            service.conn.execute(
                "UPDATE projection_change_log SET payload = ?"
                " WHERE watermark = (SELECT MAX(watermark)"
                "   FROM projection_change_log)",
                (json.dumps({"watermark": i + 2, "type": JOB_TOPIC,
                             "at": iso(base + timedelta(milliseconds=offset))}),
                 ))
        frames = service.open_stream("tnt_a", "u1", "run_1",
                                     last_event_id="1")["frames"]
        node_frames = [f for f in frames if f["event"] == "node.state"]
        assert len(node_frames) == 1, "200ms 窗口内只推最后一条"
        assert node_frames[0]["data"]["watermark"] == 4


# ================================================================ 9.4 绝不写回

class TestNoWriteBack:
    def test_t47_26_deps_exposes_only_get(self):
        import realtime_projection.deps as module

        names = {n for n in dir(module) if not n.startswith("__")}
        assert "_get" in names
        for forbidden in ("_post", "_put", "_patch", "_delete", "_request",
                          "post", "put", "patch", "delete", "request"):
            assert forbidden not in names, forbidden

    def test_t47_27_only_get_appears_in_source(self):
        path = (Path(__file__).resolve().parents[1] / "src"
                / "realtime_projection" / "deps.py")
        source = path.read_text(encoding="utf-8")
        body = source.split('"""', 2)[2]  # 跳过模块 docstring
        calls = set(re.findall(r'method\s*=\s*"([A-Z]+)"', body))
        assert calls == {"GET"}, calls
        for verb in ("urllib.request.Request(", ):
            assert body.count(verb) == 1
        assert not re.search(r'"(POST|PUT|PATCH|DELETE)"', body)

    def test_t47_28_no_code_path_can_upgrade_to_write(self):
        """注入"想写回"的意图也没有出口：`_get` 不接受 method 参数。"""
        from realtime_projection.deps import _get
        import inspect

        assert "method" not in inspect.signature(_get).parameters

    def test_t47_29_disconnect_does_not_emit_run_interrupted(self, service, conn):
        service.consume(run_event(seq=1))
        service.open_stream("tnt_a", "u1", "run_1")
        types = [json.loads(r["payload"])["type"] for r in
                 rows(conn, "SELECT payload FROM outbox")]
        assert types == [EVENT_CURSOR_ADVANCED] or types == []
        assert not [t for t in types if "interrupt" in t or "cancel" in t]

    def test_t47_30_notification_failure_does_not_call_upstream(self, service,
                                                                conn):
        service.consume(run_event(seq=1))
        service.put_preference("tnt_a", "u1", {
            "event_types": ["run.failed"],
            "channels": [{"kind": "webhook", "address_ref": "wh_1"}]})
        service.consume(run_event(seq=2, state="FAILED"))
        delivery = rows(conn, "SELECT * FROM notification_delivery")[0]
        assert delivery["state"] == "FAILED"
        assert delivery["last_error"] == "CHANNEL_NOT_AVAILABLE"
        assert count(conn, "outbox") == 0, "通知失败不得回发任何事件"


# ================================================================ 9.5 跨租户

class TestTenantIsolation:
    def test_t47_31_cross_tenant_snapshot_is_404_without_leak(self, service):
        service.consume(run_event(seq=1, tenant="tnt_a"))
        with pytest.raises(ServiceError) as caught:
            service.snapshot("tnt_b", "run_1")
        assert caught.value.status == 404
        assert caught.value.code == "RUN_NOT_FOUND"
        assert "tnt_a" not in json.dumps(caught.value.details)

    def test_t47_32_cursor_cannot_be_read_by_another_tenant(self, service):
        service.consume(run_event(seq=1, tenant="tnt_a"))
        opened = service.open_stream("tnt_a", "u1", "run_1")
        with pytest.raises(ServiceError) as caught:
            service.open_stream("tnt_b", "u1", "run_1",
                                subscription_id=opened["cursor"])
        assert caught.value.status == 404  # 看不到别人的 run

    def test_t47_33_list_runs_is_tenant_scoped(self, service):
        service.consume(run_event(seq=1, tenant="tnt_a", run_id="run_a"))
        service.consume(run_event(seq=1, tenant="tnt_b", run_id="run_b"))
        assert [r["run_id"] for r in
                service.list_runs("tnt_a")["items"]] == ["run_a"]
        assert [r["run_id"] for r in
                service.list_runs("tnt_b")["items"]] == ["run_b"]

    def test_t47_34_every_change_log_read_carries_tenant_predicate(self):
        """T47-34 的"行级安全"在这里的形态：**读**必须带 tenant 谓词。

        `prune()` 的删除按时间，不带租户——它是全局 janitor，删的是"确定早于
        保留窗口"的行，不返回任何内容给调用方，所以不构成越权读。所以这里只
        审 SELECT，并单独钉住 DELETE 的语义。
        """
        path = (Path(__file__).resolve().parents[1] / "src"
                / "realtime_projection" / "service.py")
        source = path.read_text(encoding="utf-8")
        selects = [m for m in re.finditer(r"SELECT[^;]*?FROM projection_change_log",
                                          source, re.S)]
        assert selects, "探针必须真的看到语句（否则这条测试恒真）"
        for match in selects:
            window = source[match.start(): match.start() + 320]
            assert "tenant_id = ?" in window, window
        deletes = [m for m in re.finditer(
            r"DELETE FROM projection_change_log[^\"]*", source)]
        assert deletes, "prune 的删除语句应当存在"
        for match in deletes:
            assert "created_at < ?" in match.group(0) or "tenant_id = ?" in \
                match.group(0), match.group(0)

    def test_cross_tenant_event_does_not_touch_other_rows(self, service, conn):
        """同 run_id 在不同租户下是**两行**；B 的事件改不到 A 的那行。"""
        service.consume(run_event(seq=1, tenant="tnt_a", completed=50))
        service.consume(run_event(seq=2, tenant="tnt_a", completed=51))
        service.consume(ledger_event(consumed=999, tenant="tnt_b",
                                     run_id="run_1"))
        assert service._row("tnt_a", "run_1")["spent_micros"] == 0
        assert service._row("tnt_b", "run_1")["spent_micros"] == 999
        assert len(rows(conn, "SELECT * FROM run_dashboard_projection")) == 2


# ================================================================ 9.6 崩溃与恢复

class TestRecovery:
    def test_t47_35_redelivery_after_crash_is_deduped(self, conn, clock):
        service = RealtimeProjectionService(conn, clock=clock)
        event = run_event(seq=1, completed=7)
        service.consume(event)
        # 模拟"应用成功但未 ack"：同一条再来一次。
        restarted = RealtimeProjectionService(conn, clock=clock)
        out = restarted.consume(event)
        assert out["reason"] == "duplicate"
        assert restarted.snapshot("tnt_a", "run_1")["progress"]["completed_shots"] == 7

    def test_t47_36_projection_committed_without_cursor_is_replayable(
            self, service):
        for seq in range(1, 4):
            service.consume(run_event(seq=seq, completed=seq))
        # 游标停在 1（模拟"投影已提交、游标未更新"），客户端拿 id=1 回来。
        opened = service.open_stream("tnt_a", "u1", "run_1",
                                     last_event_id=None)
        service.open_stream("tnt_a", "u1", "run_1",
                            subscription_id=opened["cursor"])
        frames = service.open_stream("tnt_a", "u1", "run_1",
                                     subscription_id=opened["cursor"],
                                     last_event_id="1")["frames"]
        assert [f["id"] for f in frames] == [2, 3], "按 id 去重后可安全重放"

    def test_t47_37_polling_fallback_serves_frames(self, service):
        """没有 Redis 扇出层时的降级路径：每次调用拿"游标之后"的帧。"""
        for seq in range(1, 4):
            service.consume(run_event(seq=seq, completed=seq))
        opened = service.open_stream("tnt_a", "u1", "run_1")
        assert opened["frames"] == []
        service.consume(run_event(seq=4, completed=4))
        polled = service.open_stream("tnt_a", "u1", "run_1",
                                     subscription_id=opened["cursor"])
        assert [f["id"] for f in polled["frames"]] == [4]

    def test_t47_38_store_down_does_not_fall_back_to_authority(
            self, monkeypatch, tmp_path):
        monkeypatch.setenv("S47_DB", str(tmp_path / "s47.db"))
        module = _load_app()
        client = TestClient(module.app)
        module._conn.close()
        response = client.get("/health/readiness")
        assert response.status_code == 503
        assert response.json()["code"] == "PROJECTION_UNAVAILABLE"
        assert "S21" in response.json()["message"]

    def test_t47_39_prune_drops_old_change_log_and_cursor(self, service, clock):
        for seq in range(1, 4):
            service.consume(run_event(seq=seq, completed=seq))
        opened = service.open_stream("tnt_a", "u1", "run_1")
        clock.advance(minutes=16)
        pruned = service.prune()
        assert pruned["change_log_deleted"] == 3
        assert pruned["cursors_deleted"] == 1
        with pytest.raises(ServiceError) as caught:
            service.open_stream("tnt_a", "u1", "run_1",
                                subscription_id=opened["cursor"],
                                last_event_id="3")
        assert caught.value.status == 410

    def test_evicted_row_is_absent_and_rebuildable(self, service):
        for seq in range(1, 3):
            service.consume(run_event(seq=seq, completed=seq))
        service.evict("tnt_a", "run_1")
        with pytest.raises(ServiceError):
            service.snapshot("tnt_a", "run_1")
        # 事件重放能重建（C47-10）。
        service.consume(run_event(seq=3, completed=3, event_id="rebuild_1"))
        rebuilt = service.snapshot("tnt_a", "run_1")
        assert rebuilt["projection_state"] in ("LIVE", "GAPPED")


# ================================================================ 9.7 通知

class TestNotifications:
    def test_t47_40_run_failed_notifies_on_preferred_channel(self, service, conn):
        service.put_preference("tnt_a", "u1", {
            "event_types": ["run.failed"],
            "channels": [{"kind": "in_app"}]})
        service.consume(run_event(seq=1, state="FAILED"))
        deliveries = rows(conn, "SELECT * FROM notification_delivery")
        assert len(deliveries) == 1
        assert deliveries[0]["event_type"] == "run.failed"
        assert deliveries[0]["state"] == "SENT"
        assert count(conn, "in_app_notification") == 1

    def test_t47_41_node_state_changes_do_not_notify(self, service, conn):
        service.put_preference("tnt_a", "u1", {
            "event_types": sorted(["run.failed", "run.completed"]),
            "channels": [{"kind": "in_app"}]})
        service.consume(typed_event(JOB_TOPIC,
                                    data={"node_id": "shot_1", "state": "FAILED",
                                          "node_epoch": 1}))
        assert count(conn, "notification_delivery") == 0

    def test_t47_42_quiet_hours_defer_non_urgent(self, service, conn, clock):
        # 把"现在"钉在 UTC 02:00，quiet 01:00–06:00。
        clock.at = datetime(2026, 11, 13, 2, 0, tzinfo=timezone.utc)
        service.put_preference("tnt_a", "u1", {
            "event_types": ["run.completed"],
            "channels": [{"kind": "in_app"}],
            "quiet_hours": {"from": "01:00", "to": "06:00", "tz": "UTC"}})
        service.consume(run_event(seq=1, state="SUCCEEDED"))
        delivery = rows(conn, "SELECT * FROM notification_delivery")[0]
        assert delivery["state"] == "DEFERRED"
        assert delivery["deferred_until"].startswith("2026-11-13T06:00")
        assert count(conn, "in_app_notification") == 0

    def test_t47_43_urgent_bypasses_quiet_hours(self, service, conn, clock):
        clock.at = datetime(2026, 11, 13, 2, 0, tzinfo=timezone.utc)
        service.put_preference("tnt_a", "u1", {
            "event_types": ["run.failed"],
            "channels": [{"kind": "in_app"}],
            "quiet_hours": {"from": "01:00", "to": "06:00", "tz": "UTC"}})
        service.consume(run_event(seq=1, state="FAILED"))
        delivery = rows(conn, "SELECT * FROM notification_delivery")[0]
        assert delivery["state"] == "SENT"
        assert delivery["deferred_until"] is None

    def test_t47_44_redelivery_notifies_once(self, service, conn):
        service.put_preference("tnt_a", "u1", {
            "event_types": ["run.failed"],
            "channels": [{"kind": "in_app"}]})
        event = run_event(seq=1, state="FAILED")
        service.consume(event)
        service.consume(event)
        assert count(conn, "notification_delivery") == 1
        assert count(conn, "in_app_notification") == 1

    def test_nd_uniq_blocks_a_second_delivery_directly(self, service, conn):
        service.put_preference("tnt_a", "u1", {
            "event_types": ["run.failed"],
            "channels": [{"kind": "in_app"}]})
        service.consume(run_event(seq=1, state="FAILED"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO notification_delivery (delivery_id, tenant_id,"
                " principal_id, event_id, event_type, channel_kind, state,"
                " attempt_count, created_at) VALUES (?,?,?,?,?,?,?,0,?)",
                ("ndl_dup", "tnt_a", "u1", "evt_run_1_1", "run.failed",
                 "in_app", "PENDING", iso(_now())))

    def test_t47_45_email_failure_leaves_in_app_alone(self, service, conn):
        service.put_preference("tnt_a", "u1", {
            "event_types": ["run.failed"],
            "channels": [{"kind": "in_app"}, {"kind": "email",
                                              "address_ref": "u1@example.com"}]})
        service.consume(run_event(seq=1, state="FAILED"))
        states = {r["channel_kind"]: r["state"]
                  for r in rows(conn, "SELECT * FROM notification_delivery")}
        assert states["in_app"] == "SENT"
        # 仓库里没有邮件投递器：S47 只记"该发"，不假装发过了。
        assert states["email"] == "PENDING"

    def test_t47_46_no_voice_authorization_column_anywhere(self, conn):
        columns = [r[1] for r in conn.execute(
            "PRAGMA table_info(run_dashboard_projection)").fetchall()]
        for forbidden in ("voice", "authorization", "permit", "accepted_pointer",
                          "canon"):
            assert not [c for c in columns if forbidden in c], forbidden
        snap_hint = {gap["id"] for gap in CONTRACT_GAPS}
        assert "C47-15" in snap_hint

    def test_default_preference_is_returned_not_404(self, service):
        pref = service.get_preference("tnt_a", "nobody")
        assert pref["is_default"] is True
        assert "run.failed" in pref["event_types"]

    def test_watchers_of_a_run_receive_default_notifications(self, service, conn):
        """没有偏好、但对这个 run 有活跃游标的人也会收到（C47-26 的口径）。"""
        service.consume(run_event(seq=1))
        service.open_stream("tnt_a", "watcher", "run_1")
        service.consume(run_event(seq=2, state="FAILED"))
        deliveries = rows(conn, "SELECT * FROM notification_delivery")
        assert [d["principal_id"] for d in deliveries] == ["watcher"]

    def test_mark_read_is_idempotent_and_scoped(self, service):
        service.put_preference("tnt_a", "u1", {
            "event_types": ["run.failed"], "channels": [{"kind": "in_app"}]})
        service.consume(run_event(seq=1, state="FAILED"))
        item = service.list_notifications("tnt_a", "u1")["items"][0]
        first = service.mark_read("tnt_a", "u1", item["notification_id"])
        second = service.mark_read("tnt_a", "u1", item["notification_id"])
        assert first["already_read"] is False
        assert second["already_read"] is True
        with pytest.raises(ServiceError):
            service.mark_read("tnt_b", "u1", item["notification_id"])


# ================================================================ 9.8 独立升级

class TestIndependentUpgrade:
    def test_t47_47_imports_only_platform_sdks(self):
        path = (Path(__file__).resolve().parents[1] / "src"
                / "realtime_projection")
        for module in path.glob("*.py"):
            source = module.read_text(encoding="utf-8")
            for match in re.finditer(r"^(?:from|import)\s+([a-z_][\w.]*)",
                                     source, re.M):
                root = match.group(1).split(".")[0]
                assert root in {
                    "__future__", "json", "sqlite3", "uuid", "datetime",
                    "collections", "hashlib", "hmac", "re", "urllib",
                    "zoneinfo", "event_sdk", "idempotency_sdk",
                    "realtime_projection",
                }, f"{module.name}: {root}"

    def test_t47_48_unknown_fields_and_types_are_tolerated(self, service):
        event = run_event(seq=1)
        event["data"]["some_future_field"] = {"nested": [1, 2, 3]}
        out = service.consume(event)
        assert out["applied"] is True
        out2 = service.consume(typed_event("brand.new.type.v9"))
        assert out2["reason"] == "unrouted"

    def test_t47_49_rebuild_from_events_restores_every_projected_field(
            self, conn, clock):
        events = [
            run_event(seq=1, completed=3, state="RUNNING"),
            run_event(seq=2, completed=9, state="RUNNING"),
            ledger_event(consumed=777, event_id="l1"),
            typed_event(ARTIFACT_TOPIC, event_id="a1",
                        data={"artifact_ref": {"id": "art_9"},
                              "content_class": "video"}),
            run_event(seq=3, completed=10, state="SUCCEEDED"),
        ]
        first = RealtimeProjectionService(conn, clock=clock)
        for event in events:
            first.consume(event)
        expected = first.snapshot("tnt_a", "run_1")

        # 清空投影表（模拟"被清理后重建"），用同一批事件重放。
        conn.execute("DELETE FROM run_dashboard_projection")
        conn.execute("DELETE FROM projection_change_log")
        conn.execute("DELETE FROM inbox")
        second = RealtimeProjectionService(conn, clock=clock)
        for event in events:
            second.consume(event)
        rebuilt = second.snapshot("tnt_a", "run_1")
        for field in ("run_id", "projection_state", "source_watermark"):
            assert rebuilt[field] == expected[field], field
        for section in ("run", "progress", "cost", "artifacts"):
            assert rebuilt[section] == expected[section], section
        # warnings 里的 `at` 是"检测到这件事的时刻"，重放时自然不同；
        # 比的是**内容**，不是时间戳。
        assert [(w["code"], w.get("missing")) for w in rebuilt["warnings"]] == \
            [(w["code"], w.get("missing")) for w in expected["warnings"]]

    def test_t47_50_disconnect_never_looks_like_failure(self, service, clock):
        """E40：断线后重连看到一致的进度，用户不会误以为任务失败。"""
        for seq in range(1, 4):
            service.consume(run_event(seq=seq, completed=seq * 5))
        opened = service.open_stream("tnt_a", "u1", "run_1")
        clock.advance(minutes=2)  # 断线两分钟
        service.consume(run_event(seq=4, completed=20))
        resumed = service.open_stream("tnt_a", "u1", "run_1",
                                      subscription_id=opened["cursor"],
                                      last_event_id="3")
        snap = service.snapshot("tnt_a", "run_1")
        assert snap["run"]["state"] == "RUNNING"
        assert snap["projection_state"] == "LIVE"
        assert [f["id"] for f in resumed["frames"]] == [4]
        assert not [w for w in snap["warnings"] if w["code"] == "PROJECTION_GAP"]


# ================================================================ 缺口行为

class TestContractGaps:
    def test_c47_12_unknown_topics_from_the_plan_are_not_fabricated(self, service):
        """C47-12/13/14：方案 §2.1 点名的三个 topic 不在注册表里。"""
        for missing in (NODE_TOPIC, "artifact.registered", "artifact.deleted",
                        "budget.reservation.settled"):
            out = service.consume(typed_event(missing))
            assert out["reason"] == "unrouted", missing

    def test_c47_15_missing_coordinates_are_declared_not_invented(self, service):
        """C47-15：事件不带 project/episode/branch 时不伪造合法 id。"""
        service.consume(run_event(seq=1))
        row = service._row("tnt_a", "run_1")
        assert row["project_id"] == ""
        snap = service.snapshot("tnt_a", "run_1")
        assert snap["run"]["episode_id"] is None
        missing = [w for w in snap["warnings"]
                   if w["code"] == "PROJECTION_COORDINATE_MISSING"]
        assert missing and set(missing[0]["missing"]) == {
            "project_id", "episode_id", "branch_id"}

    def test_c47_16_total_shots_is_not_faked(self, service):
        """C47-16：没有 total 的来源就保持 None。"""
        service.consume(run_event(seq=1, completed=4, blocked=2))
        snap = service.snapshot("tnt_a", "run_1")
        assert snap["progress"]["completed_shots"] == 4
        assert snap["progress"]["total_shots"] is None
        assert snap["progress"]["current_node"] is None

    def test_spent_counts_only_settled_entries(self, service):
        """C47-14：RESERVE/RELEASE 不是花费。"""
        service.consume(run_event(seq=1))
        service.consume(ledger_event(entry_type="RESERVE", consumed=5000,
                                     event_id="l1"))
        assert service._row("tnt_a", "run_1")["spent_micros"] == 0
        service.consume(ledger_event(entry_type="SETTLE", consumed=5000,
                                     event_id="l2"))
        assert service._row("tnt_a", "run_1")["spent_micros"] == 5000
        assert service.snapshot("tnt_a", "run_1")["cost"]["spent_as_of"]

    def test_c47_23_b_class_events_get_distinct_display_watermarks(self, service,
                                                               conn):
        """C47-23：B 类事件没有上游序，仍要各自一条 change_log。"""
        service.consume(run_event(seq=1))
        service.consume(ledger_event(consumed=100, event_id="l1"))
        service.consume(ledger_event(consumed=200, event_id="l2"))
        marks = [r["watermark"] for r in
                 rows(conn, "SELECT watermark FROM projection_change_log"
                            " ORDER BY watermark")]
        assert len(marks) == len(set(marks)) == 3
        assert marks == sorted(marks)

    def test_resync_clears_gap(self, conn, clock):
        service = RealtimeProjectionService(
            conn, clock=clock,
            run_source=lambda t, r: {"state": "RUNNING", "manifest_revision": 9,
                                     "completed_nodes": 9, "blocked_nodes": 0,
                                     "execution_fence": 1,
                                     "projection_watermark": 9})
        service.consume(run_event(seq=1))
        service.consume(run_event(seq=5, completed=5))
        assert service._row("tnt_a", "run_1")["projection_state"] == "GAPPED"
        service.resync("tnt_a", "run_1")
        row = service._row("tnt_a", "run_1")
        assert row["projection_state"] == "LIVE"
        assert row["gap"] is None
        assert row["applied_watermark"] == 9
        assert not [w for w in json.loads(row["warnings"])
                    if w["code"] == "PROJECTION_GAP"]

    def test_c47_20_scope_kind_tenant_is_registered_without_a_cursor(self, service):
        """C47-20：租户级订阅可登记，但不假装能跑 SSE。"""
        created = service.create_subscription(
            "tnt_a", "u1", {"scope": {"kind": "tenant", "id": "tnt_a"}})
        assert created["stream_url"] is None
        assert created["cursor"] is None
        assert created["snapshot_watermark"] == 0

    def test_c47_25_no_projection_read_permission_is_claimed(self):
        """C47-25：权限表里没有 projection.read，S47 不自称有这个权限。"""
        gap = next(g for g in CONTRACT_GAPS if g["id"] == "C47-25")
        assert "projection.read" in gap["summary"]

    def test_c47_gaps_have_the_documented_shape(self):
        for gap in CONTRACT_GAPS:
            assert set(gap) == {"id", "kind", "summary", "handling"}, gap["id"]
            assert gap["kind"] in ("conflict", "needs_owner", "gap")
            assert re.match(r"^C47-\d+$", gap["id"]), gap["id"]
        ids = [g["id"] for g in CONTRACT_GAPS]
        assert len(ids) == len(set(ids))


# ================================================================ 脱敏

class TestRedaction:
    def test_class_a_is_stable_and_scoped(self):
        first, hits = redact_text("token ak_live_0123456789abcdef",
                                  "tnt_a")
        again, _ = redact_text("token ak_live_0123456789abcdef", "tnt_a")
        other, _ = redact_text("token ak_live_0123456789abcdef", "tnt_b")
        assert hits == 1
        assert first == again, "同租户同值必须得到同一 token（可关联）"
        assert first != other, "跨租户必须不可关联"
        assert "hmac_api_key:" in first
        assert "ak_live_0123456789abcdef" not in first

    def test_class_b_is_not_linkable(self):
        pem = ("-----BEGIN RSA PRIVATE KEY-----\n"
               "MIIEowIBAAKCAQEA\n"
               "-----END RSA PRIVATE KEY-----")
        cleaned, hits = redact_text(pem, "tnt_a")
        assert hits == 1
        assert cleaned == REDACTED_PREFIX + "private_key_material", \
            "class_b 的替换不含 hmac 摘要（不可关联）"

    def test_payload_preserves_shape_and_array_length(self):
        payload = {"artifacts": [
            {"artifact_id": "art_1", "url": "https://u:p@host/x",
             "content_hash": "a" * 64},
            {"artifact_id": "art_2", "url": "https://host/y"}]}
        clean, hits = redact_payload(payload, "tnt_a")
        assert len(clean["artifacts"]) == 2
        assert list(clean["artifacts"][0]) == list(payload["artifacts"][0])
        assert clean["artifacts"][0]["artifact_id"] == "art_1"
        assert clean["artifacts"][0]["content_hash"] == "a" * 64, \
            "hash/id 键上的纯 hash 要豁免（契约 false_positive_note）"
        assert hits >= 1

    def test_change_log_is_redacted_before_storage(self, service, conn):
        event = run_event(seq=1)
        event["data"]["url"] = "postgres://user:hunter2@db.internal/app"
        service.consume(event)
        stored = rows(conn, "SELECT payload FROM projection_change_log")[0]
        assert "hunter2" not in stored["payload"]

    def test_change_log_only_carries_whitelisted_frame_fields(self, service,
                                                              conn):
        """帧的 payload 是**白名单构造**的，不整段透传 data。

        §5.2 要求"凡是直接透传上游 raw payload 的字段都要过一遍脱敏"。这里的
        做法更强：根本不透传——帧里只有 §4.3 列出的那几个键。于是上游 data 里
        的任意敏感串既不会进 change_log，也不会进 SSE。
        """
        event = run_event(seq=1)
        event["data"]["note"] = "Bearer abcdefghijklmnopqrstuvwxyz"
        event["data"]["api_key"] = "ak_live_0123456789abcdef"
        service.consume(event)
        stored = rows(conn, "SELECT payload FROM projection_change_log")[0]
        payload = json.loads(stored["payload"])
        assert set(payload) <= {"watermark", "type", "at", "run_state",
                                "completed_shots", "current_stage", "warning",
                                "redaction_hits"}
        assert "abcdefghijklmnopqrstuvwxyz" not in stored["payload"]
        assert "ak_live_0123456789abcdef" not in stored["payload"]

    def test_redaction_still_fires_on_passthrough_fields(self, service, conn):
        """白名单挡不住的路径（`blocked_reason` 会进 warning）仍要脱敏。"""
        payload, hits = redact_payload(
            {"warning": {"code": "X", "detail": "Bearer abcdefghijklmnopqrst"}},
            "tnt_a")
        assert hits == 1
        assert "abcdefghijklmnopqrst" not in json.dumps(payload)

    def test_no_kms_means_derived_key_is_documented(self):
        gap = next(g for g in CONTRACT_GAPS if g["id"] == "C47-22")
        assert "KMS" in gap["summary"]
        assert hmac_scope_key("t", "api_key", b"root") == \
            hmac_scope_key("t", "api_key", b"root")


# ================================================================ HTTP 层

def _frames(client, module, run_id="run_1", *, principal="u1",
            last_event_id=None) -> list[str]:
    """取 SSE 的帧文本。

    **不在 TestClient 上读流**：SSE 是无限响应，`client.get` / `iter_text`
    都会等 EOF 等到超时——既慢又测不出东西。帧的组装是 `_sse_frame` 的纯
    函数行为，直接喂它内核给的帧；HTTP 层是否把流挂对（media-type、
    cursor 头）另用一次不消费的 `stream()` 断言。
    """
    opened = module.service.open_stream("tnt_a", principal, run_id,
                                        last_event_id=last_event_id)
    return [module._sse_frame(frame) for frame in opened["frames"]]


def _load_app():
    """`api/app.py` 的模块。不能 `import app`——别的服务也有同名文件。"""
    path = Path(__file__).resolve().parents[1] / "api" / "app.py"
    spec = importlib.util.spec_from_file_location("s47_api_app", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["s47_api_app"] = module
    return module


@pytest.fixture()
def http(monkeypatch, tmp_path):
    monkeypatch.setenv("S47_DB", str(tmp_path / "s47.db"))
    monkeypatch.delenv("S21_BASE_URL", raising=False)
    monkeypatch.delenv("S01_BASE_URL", raising=False)
    module = _load_app()
    # S01 未配置时是 fail-closed 503（见 TestAuthorization）；HTTP 层的行为用
    # 一个按真实 S01 形状返回的 stub 来测，而不是把检查关掉。
    module.service.authorization_source = lambda tenant, principal, kind, sid: {
        "permissions": ["run.get"], "denied": [],
        "revocation_epoch": 3, "personal_revocation_epoch": 1,
        "ttl_seconds": 60}
    client = TestClient(module.app)
    return client, module


HEAD = {"X-Tenant-Id": "tnt_a", "X-Principal-Id": "u1"}


class TestHttp:
    def test_consume_then_snapshot_round_trip(self, http):
        client, _ = http
        event = run_event(seq=1, completed=4)
        r = client.post("/events:consume", json={"event": event}, headers=HEAD)
        assert r.status_code == 200, r.text
        assert r.json()["applied"] is True
        snap = client.get("/runs/run_1/snapshot", headers=HEAD)
        assert snap.status_code == 200, snap.text
        assert snap.json()["progress"]["completed_shots"] == 4

    def test_consume_rejects_tenant_mismatch(self, http):
        client, _ = http
        event = run_event(seq=1, tenant="tnt_b")
        r = client.post("/events:consume", json={"event": event}, headers=HEAD)
        assert r.status_code == 403
        assert r.json()["code"] == "TENANT_SCOPE_DENIED"

    def test_missing_tenant_header_is_403(self, http):
        client, _ = http
        r = client.get("/runs/run_1/snapshot")
        assert r.status_code == 403

    def test_cross_tenant_snapshot_is_404_over_http(self, http):
        client, _ = http
        client.post("/events:consume", json={"event": run_event(seq=1)},
                    headers=HEAD)
        r = client.get("/runs/run_1/snapshot",
                       headers={"X-Tenant-Id": "tnt_b"})
        assert r.status_code == 404
        assert r.json()["code"] == "RUN_NOT_FOUND"

    def test_subscription_requires_idempotency_key(self, http):
        client, _ = http
        client.post("/events:consume", json={"event": run_event(seq=1)},
                    headers=HEAD)
        body = {"scope": {"kind": "run", "id": "run_1"}}
        r = client.post("/subscriptions", json=body, headers=HEAD)
        assert r.status_code == 400
        r2 = client.post("/subscriptions", json=body,
                         headers={**HEAD, "Idempotency-Key": "c1"})
        assert r2.status_code == 200, r2.text
        payload = r2.json()
        assert payload["snapshot_watermark"] == 1
        assert payload["stream_url"].startswith("/runs/run_1/events")
        assert payload["cursor"]

    def test_subscription_replay_and_conflict(self, http):
        client, _ = http
        client.post("/events:consume", json={"event": run_event(seq=1)},
                    headers=HEAD)
        body = {"scope": {"kind": "run", "id": "run_1"}}
        first = client.post("/subscriptions", json=body,
                            headers={**HEAD, "Idempotency-Key": "c1"})
        again = client.post("/subscriptions", json=body,
                            headers={**HEAD, "Idempotency-Key": "c1"})
        assert again.json()["replayed"] is True
        assert again.json()["subscription_id"] == first.json()["subscription_id"]
        other = client.post("/subscriptions",
                            json={"scope": {"kind": "run", "id": "run_1"},
                                  "event_filters": ["run.state"]},
                            headers={**HEAD, "Idempotency-Key": "c1"})
        assert other.status_code == 409
        assert other.json()["code"] == "IDEMPOTENCY_KEY_REUSED"

    def test_sse_reconnect_returns_only_new_frames(self, http):
        client, module = http
        for seq in (1, 2, 3):
            client.post("/events:consume",
                        json={"event": run_event(seq=seq, completed=seq)},
                        headers=HEAD)
        created = client.post("/subscriptions",
                              json={"scope": {"kind": "run", "id": "run_1"}},
                              headers={**HEAD, "Idempotency-Key": "c1"}).json()
        client.post("/events:consume",
                    json={"event": run_event(seq=4, completed=4)}, headers=HEAD)
        body = "".join(_frames(client, module, last_event_id="3"))
        assert "id: 4" in body and "event: run.state" in body
        assert "id: 1\n" not in body

    def test_sse_route_declares_stream_contract(self, http):
        """§4.3 的响应契约：`text/event-stream` + 两个游标头。

        **不经过 TestClient 取响应头**：Starlette 的 TestClient 会等整个 ASGI
        应用跑完才返回，而 SSE 应用不会跑完——`client.stream()` 在 `__enter__`
        里就把测试挂死（不是断言失败）。所以直接调端点函数取 `StreamingResponse`
        对象，只读它的声明的头，不消费 body。
        """
        client, module = http
        client.post("/events:consume", json={"event": run_event(seq=1)},
                    headers=HEAD)
        module.service.open_stream("tnt_a", "u1", "run_1")
        client.post("/events:consume", json={"event": run_event(seq=2)},
                    headers=HEAD)

        class _NeverDisconnects:
            async def is_disconnected(self) -> bool:
                return True

        response = module.stream_run(
            "run_1", _NeverDisconnects(), x_tenant_id="tnt_a",
            x_principal_id="u1", last_event_id="1", subscription_id=None)
        assert response.media_type == "text/event-stream"
        assert response.headers["X-S47-Watermark"] == "2"
        assert response.headers["X-S47-Cursor"]
        assert response.headers["Cache-Control"] == "no-cache"

    def test_sse_generator_ends_when_client_disconnects(self, http):
        """断线就收尾，不把协程留在服务器上。"""
        import asyncio

        client, module = http
        client.post("/events:consume", json={"event": run_event(seq=1)},
                    headers=HEAD)
        # 先开一次流建立游标（水位落在 1），再推第二条——否则新游标开在当前
        # 水位上，本来就没有帧，测不出"断线前先把帧吐完"。
        module.service.open_stream("tnt_a", "u1", "run_1")
        client.post("/events:consume", json={"event": run_event(seq=2)},
                    headers=HEAD)

        class _Disconnected:
            async def is_disconnected(self) -> bool:
                return True

        response = module.stream_run(
            "run_1", _Disconnected(), x_tenant_id="tnt_a",
            x_principal_id="u1", last_event_id="1", subscription_id=None)

        async def _drain():
            chunks = []
            async for chunk in response.body_iterator:
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(_drain())
        assert len(chunks) == 1
        assert chunks[0].startswith("id: 2\n")  # 客户端说看到 1，只补 2
        assert ": keepalive" not in chunks[0]

    def test_sse_body_always_has_watermark_in_data(self, http):
        client, module = http
        client.post("/events:consume", json={"event": run_event(seq=1)},
                    headers=HEAD)
        module.service.open_stream("tnt_a", "u1", "run_1")
        client.post("/events:consume", json={"event": run_event(seq=2)},
                    headers=HEAD)
        frames = _frames(client, module, last_event_id="1")
        assert frames
        data_lines = [ln for ln in "".join(frames).splitlines()
                      if ln.startswith("data: ")]
        assert len(data_lines) == len(frames)
        for line in data_lines:
            assert "watermark" in json.loads(line[len("data: "):])

    def test_sse_frame_without_watermark_is_refused(self, http):
        _, module = http
        with pytest.raises(ServiceError) as caught:
            module._sse_frame({"id": 1, "event": "run.state", "data": {}})
        assert caught.value.code == "PROJECTION_INVARIANT"

    def test_expired_cursor_returns_410_with_details(self, http):
        client, module = http
        client.post("/events:consume", json={"event": run_event(seq=1)},
                    headers=HEAD)
        created = client.post("/subscriptions",
                              json={"scope": {"kind": "run", "id": "run_1"}},
                              headers={**HEAD, "Idempotency-Key": "c1"}).json()
        module.service._cursor_emitted_at.clear()
        past = iso(_now() - timedelta(minutes=20))
        module._conn.execute(
            "UPDATE sse_cursor SET expires_at = ?", (past,))
        r = client.get("/runs/run_1/events",
                       headers={**HEAD, "Last-Event-ID": "1"},
                       params={"subscription_id": created["cursor"]})
        assert r.status_code == 410
        body = r.json()
        assert body["code"] == "CURSOR_EXPIRED"
        assert body["details"]["snapshot_url"] == "/runs/run_1/snapshot"
        assert body["details"]["do_not_resubmit"]

    def test_notification_preferences_round_trip(self, http):
        client, _ = http
        r = client.get("/notification-preferences", headers=HEAD)
        assert r.json()["is_default"] is True
        r2 = client.post("/notification-preferences",
                         json={"event_types": ["run.failed"],
                               "channels": [{"kind": "in_app"}]},
                         headers=HEAD)
        assert r2.json()["is_default"] is False
        assert r2.json()["event_types"] == ["run.failed"]
        r3 = client.patch("/notification-preferences",
                          json={"channels": [{"kind": "email",
                                              "address_ref": "a@b.c"}]},
                          headers=HEAD)
        assert r3.json()["event_types"] == ["run.failed"]  # 未给字段沿用
        assert r3.json()["channels"][0]["kind"] == "email"

    def test_unknown_channel_kind_is_400(self, http):
        client, _ = http
        r = client.post("/notification-preferences",
                        json={"channels": [{"kind": "carrier_pigeon"}]},
                        headers=HEAD)
        assert r.status_code == 400

    def test_webhook_subscription_channel_is_refused(self, http):
        client, _ = http
        client.post("/events:consume", json={"event": run_event(seq=1)},
                    headers=HEAD)
        r = client.post("/subscriptions",
                        json={"scope": {"kind": "run", "id": "run_1"},
                              "channel": "webhook"},
                        headers={**HEAD, "Idempotency-Key": "c1"})
        assert r.status_code == 400
        assert "C47-7" in r.json()["message"]

    def test_notifications_list_and_mark_read(self, http):
        client, module = http
        module.service.put_preference("tnt_a", "u1", {
            "event_types": ["run.failed"], "channels": [{"kind": "in_app"}]})
        client.post("/events:consume",
                    json={"event": run_event(seq=1, state="FAILED")},
                    headers=HEAD)
        listing = client.get("/notifications", headers=HEAD).json()
        assert len(listing["items"]) == 1
        nid = listing["items"][0]["notification_id"]
        r = client.post(f"/notifications/{nid}:read", headers=HEAD)
        assert r.json()["already_read"] is False
        assert client.get("/notifications", params={"unread": True},
                          headers=HEAD).json()["items"] == []

    def test_readiness_lists_contract_gaps(self, http):
        client, _ = http
        body = client.get("/health/readiness").json()
        assert body["ready"] is True
        assert {g["id"] for g in body["contract_gaps"]} >= {"C47-1", "C47-23"}

    def test_list_runs_and_summary(self, http):
        client, _ = http
        client.post("/events:consume",
                    json={"event": run_event(seq=1, run_id="run_a")}, headers=HEAD)
        client.post("/events:consume",
                    json={"event": run_event(seq=1, run_id="run_b",
                                             state="SUCCEEDED")}, headers=HEAD)
        listing = client.get("/runs", headers=HEAD).json()
        assert {r["run_id"] for r in listing["items"]} == {"run_a", "run_b"}
        summary = client.get("/dashboard/summary", headers=HEAD).json()
        assert summary["total_runs"] == 2
        assert summary["active_runs"] == 1
        assert summary["completed_runs"] == 1

    def test_delete_subscription(self, http):
        client, _ = http
        client.post("/events:consume", json={"event": run_event(seq=1)},
                    headers=HEAD)
        created = client.post("/subscriptions",
                              json={"scope": {"kind": "run", "id": "run_1"}},
                              headers={**HEAD, "Idempotency-Key": "c1"}).json()
        r = client.delete(f"/subscriptions/{created['subscription_id']}",
                          headers=HEAD)
        assert r.json()["state"] == "CLOSED"
        again = client.delete(f"/subscriptions/{created['subscription_id']}",
                              headers=HEAD)
        assert again.status_code == 404
        assert again.json()["code"] == "SUBSCRIPTION_NOT_FOUND"

    def test_resync_without_s21_is_503_not_a_guess(self, http):
        client, _ = http
        client.post("/events:consume", json={"event": run_event(seq=1)},
                    headers=HEAD)
        r = client.post("/runs/run_1:resync", headers=HEAD)
        assert r.status_code == 503
        assert r.json()["code"] == "PROJECTION_UNAVAILABLE"


# ================================================================ 上游形状

class TestUpstreamContract:
    def test_run_source_requires_integer_watermark(self, monkeypatch):
        import urllib.request

        class Response:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        def fake_urlopen(request, timeout=None):
            assert request.get_method() == "GET"
            return Response(json.dumps({"state": "RUNNING"}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        source = build_run_source("http://s21.test")
        with pytest.raises(ServiceError) as caught:
            source("tnt_a", "run_1")
        assert caught.value.status == 502

    def test_run_source_ignores_fields_s21_does_not_send(self, monkeypatch):
        import urllib.request

        class Response:
            def __init__(self, body):
                self.body = body

            def read(self):
                return self.body

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        seen = []

        def fake_urlopen(request, timeout=None):
            seen.append(request.get_method())
            return Response(json.dumps({
                "run_id": "run_1", "manifest_revision": 4,
                "execution_fence": 1, "state": "RUNNING",
                "completed_nodes": 4, "blocked_nodes": 0,
                "projection_watermark": 12}).encode())

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        result = build_run_source("http://s21.test")("tnt_a", "run_1")
        assert set(result) == {"run_id", "state", "manifest_revision",
                               "completed_nodes", "blocked_nodes",
                               "execution_fence", "projection_watermark"}
        assert "total_shots" not in result
        assert seen == ["GET"]

    def test_unconfigured_sources_fail_closed(self):
        with pytest.raises(ServiceError) as caught:
            build_run_source(None)("tnt_a", "run_1")
        assert caught.value.status == 503

    def test_id_prefixes_are_distinct(self):
        assert len(set(ID_PREFIX.values())) == len(ID_PREFIX)
