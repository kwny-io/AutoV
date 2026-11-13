"""S22 scheduler-quota 测试。编号对应 plans/S22-scheduler-quota.md §7。

全部计数都是整数，没有 float 参与配额比较。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

import pytest

from scheduler_quota.schema import SCHEMA
from scheduler_quota.service import SchedulerQuotaService, ServiceError

ROOT = Path(__file__).resolve().parents[1]
T = "tenant_a"
U = "tenant_b"
FAR = "2099-01-01T00:00:00.000000Z"


def _conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.executescript(SCHEMA)
    return c


def _svc(conn=None, *, clock=None):
    kwargs = {}
    if clock is not None:
        kwargs["clock"] = clock
    return SchedulerQuotaService(conn or _conn(), **kwargs)


def _pool(svc, *, limit=2, cap=3, rpm=100, rpm_remaining=100, modality="video",
          provider="p1", channel="c1", account="acct1", region="cn",
          resource_class="api_request", tid=T, scope="shared", pool_id=None):
    body = {
        "provider_id": provider, "channel_id": channel, "account_ref": account,
        "region": region, "modality": modality, "tenant_scope": scope,
        "resource_class": resource_class, "inflight_limit": limit,
        # 分片的账号上限是账号级、跨池共享，只有建池时才定；同分片的第二个池
        # 带不同的 cap 会被 SHARD_CAP_CONFLICT 拒掉（不是我们想测的东西）。
        "account_cap_inflight": cap,
    }
    if rpm is not None:
        body["rpm_limit"] = rpm
        body["rpm_remaining_window"] = rpm_remaining
    if pool_id:
        body["pool_id"] = pool_id
    return svc.create_pool(tid, body, actor={"service": "test"},
                           correlation_id="pool")["pool_id"]


def _route(pool_id=None, *, provider="p1", channel="c1", region="cn",
           modality="video"):
    body = {"provider_id": provider, "channel_id": channel, "region": region,
            "modality": modality,
            "route_decision_ref": {"service_id": "S25", "id": "route_1",
                                   "revision": 1}}
    if pool_id:
        body["requested_pool_id"] = pool_id
    return body


def _acquire_body(attempt="a1", *, fence=1, pool_id=None, amount=1000,
                  key=None, **route_kw):
    body = {
        "reservation_ref": f"res_{attempt}",
        "reservation_amount_micro": amount,
        "budget_scope_id": "budget_ep3",
        "attempt_id": attempt,
        "execution_fence": fence,
        **_route(pool_id, **route_kw),
    }
    if key is not None:
        body["deductible_key"] = key
    return body


def _acquire(svc, body, *, tid=T, cid=None):
    return svc.acquire(tid, body, actor={"service": "test"},
                       correlation_id=cid or body["attempt_id"])


def _confirm_once(svc, permit_id, epoch, *, dispatch_id="disp_1", tid=T, cid="d"):
    """§2.2 单次 confirm-dispatch。第一次只到 DISPATCH_AUTHORIZED。"""
    return svc.confirm_dispatch(
        tid, permit_id,
        {"expected_grant_epoch": epoch, "dispatch_id": dispatch_id,
         "reservation_still_authorized": True},
        actor={"service": "test"}, correlation_id=cid)


def _dispatch(svc, permit_id, epoch, *, dispatch_id="disp_1", tid=T, cid="d"):
    """S26 的完整两次确认：RESERVED → DISPATCH_AUTHORIZED → INFLIGHT。"""
    first = _confirm_once(svc, permit_id, epoch, dispatch_id=dispatch_id,
                          tid=tid, cid=cid)
    assert first["state"] == "DISPATCH_AUTHORIZED", first
    return _confirm_once(svc, permit_id, epoch, dispatch_id=dispatch_id,
                         tid=tid, cid=cid + "-2")


def _release(svc, permit_id, reason="SETTLED", *, tid=T, cid="r",
             evidence=None, epoch=None):
    body = {"reason": reason,
            "terminal_evidence_ref": evidence or {"service_id": "S26",
                                                  "id": "ev1", "revision": 5}}
    if epoch is not None:
        body["expected_grant_epoch"] = epoch
    return svc.release(tid, permit_id, body, actor={"service": "test"},
                       correlation_id=cid)


def _counts(svc, pool_id, tid=T):
    p = svc.get_pool(tid, pool_id)
    return p["inflight"]["used"], p["account"]["used_inflight"]


# ============================================================ 7.1 幂等与版本

def test_t22_01_same_command_same_body_replays():
    """T22-01[E12] 同 command_id 同 body ×2 → 同一 permit，计数只 +1，Outbox 1 条。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5)
    body = _acquire_body("a1")
    first = _acquire(svc, body, cid="cmd1")
    second = _acquire(svc, body, cid="cmd1")
    assert first["permit_id"] == second["permit_id"]
    assert _counts(svc, pid) == (1, 1)
    reserved = svc.conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE payload LIKE '%\"RESERVED\"%'"
    ).fetchone()["n"]
    assert reserved == 1


def test_t22_02_same_command_different_body_conflicts():
    """T22-02[E12] 同 command_id 不同 body → 由 HTTP 层 409；服务层是另一把闸。"""
    from idempotency_sdk import CommandConflict, SqliteIdempotencyStore, canonical_request_hash
    svc = _svc()
    _pool(svc, limit=5, cap=5)
    idem = SqliteIdempotencyStore(svc.conn)
    a = {"attempt_id": "a1", "x": 1}
    b = {"attempt_id": "a1", "x": 2}
    idem.begin(T, "cmd1", canonical_request_hash(a))
    with pytest.raises(CommandConflict):
        idem.begin(T, "cmd1", canonical_request_hash(b))


def test_t22_03_different_command_same_deductible_key_replays():
    """T22-03 不同 command_id 相同 deductible_key → 同一 permit，不占第二份。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5)
    body = _acquire_body("a1", key="attempt:a1")
    first = _acquire(svc, body, cid="cmd1")
    second = _acquire(svc, body, cid="cmd2")
    assert first["permit_id"] == second["permit_id"]
    assert second["replayed"] is True
    assert _counts(svc, pid) == (1, 1)


def test_t22_04_same_key_different_attempt_conflicts():
    """T22-04 同 deductible_key 不同 attempt_id → 422 DEDUCTIBLE_KEY_CONFLICT。"""
    svc = _svc()
    _pool(svc, limit=5, cap=5)
    _acquire(svc, _acquire_body("a1", key="shared"), cid="cmd1")
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, _acquire_body("a2", key="shared"), cid="cmd2")
    assert exc.value.code == "DEDUCTIBLE_KEY_CONFLICT"
    assert exc.value.status == 422


def test_t22_05_expired_permit_does_not_block_reacquire():
    """T22-05 permit 已 EXPIRED 后同 deductible_key 重 acquire → 允许新建。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5)
    body = _acquire_body("a1", key="k1")
    first = _acquire(svc, body, cid="cmd1")
    svc.expire(first["permit_id"], actor={"service": "janitor"},
               correlation_id="j1")
    assert _counts(svc, pid) == (0, 0)
    second = _acquire(svc, body, cid="cmd2")
    assert second["permit_id"] != first["permit_id"]
    assert _counts(svc, pid) == (1, 1)
    assert second["replayed"] is False


# ============================================================ 7.2 配额守恒

def test_t22_06_concurrent_acquire_grants_exactly_limit(tmp_path):
    """T22-06[E20] limit=5，并发 100 个 acquire → 恰好 5 个成功。"""
    db = tmp_path / "q.db"
    setup = sqlite3.connect(db, isolation_level=None)
    setup.executescript(SCHEMA)
    boot = SchedulerQuotaService(setup)
    pid = _pool(boot, limit=5, cap=5, rpm=None)
    setup.close()

    granted, refused = [], []
    lock = threading.Lock()

    def worker(i):
        conn = sqlite3.connect(db, isolation_level=None, timeout=30)
        svc = SchedulerQuotaService(conn)
        try:
            r = svc.acquire(T, _acquire_body(f"a{i}"),
                            actor={"service": "test"}, correlation_id=f"c{i}")
            with lock:
                granted.append(r)
        except ServiceError as exc:
            with lock:
                refused.append(exc.code)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(granted) == 5, f"granted {len(granted)}"
    assert set(refused) == {"NO_CAPACITY"}
    check = _svc(sqlite3.connect(db, isolation_level=None))
    assert _counts(check, pid) == (5, 5)
    # 无任何时刻 >5：最终值即峰值，因为只增不减。
    assert check.get_pool(T, pid)["inflight"]["used"] == 5


def test_t22_07_account_cap_spans_modalities():
    """T22-07[E20] 两个 modality 池共享账号，cap=10，两池各 limit=8 → 总授予 ≤10。

    注意：单个池的 limit 已经占满 cap 时（8 + 8 > 10），配置阶段就会被
    §1.1 那条 Σ limit ≤ cap 拒掉。所以这里两池的 limit 之和必须 ≤ cap，
    验收点落在**运行时的账号级上限**上：两池各自还有余量，但账号先满。
    """
    svc = _svc()
    video = _pool(svc, limit=5, cap=10, modality="video", rpm=None)
    image = _pool(svc, limit=5, cap=10, modality="image", rpm=None)
    assert svc.get_pool(T, video)["account"]["cap_inflight"] == 10

    outcomes = []
    for i in range(8):
        try:
            outcomes.append(("ok", _acquire(
                svc, _acquire_body(f"a{i}", modality="video"), cid=f"cv{i}")))
        except ServiceError as exc:
            outcomes.append((exc.code, exc.details.get("bottleneck")))
    for i in range(8):
        try:
            outcomes.append(("ok", _acquire(
                svc, _acquire_body(f"b{i}", modality="image"), cid=f"ci{i}")))
        except ServiceError as exc:
            outcomes.append((exc.code, exc.details.get("bottleneck")))

    ok = [o for o in outcomes if o[0] == "ok"]
    assert len(ok) == 10, f"granted {len(ok)}"
    # 两池的池级计数之和不得超过账号上限，单池也不得超过账号上限。
    used_v = svc.get_pool(T, video)["inflight"]["used"]
    used_i = svc.get_pool(T, image)["inflight"]["used"]
    assert used_v + used_i == 10
    assert max(used_v, used_i) <= 10
    # 拒绝必须发生在**账号维度**：两池各 5 个槽，池维度的 5 先满，账号维度
    # 的 10 后满。所以前 5 次拒绝报 inflight（自己那个池满了），后 3 次报
    # account（账号已满，另一个池还有空位）。
    refused = [o for o in outcomes if o[0] != "ok"]
    assert refused and all(code == "NO_CAPACITY" for code, _ in refused)
    assert [why for _, why in refused] == ["inflight"] * 3 + ["account"] * 3
    # 同一次拒绝里 `account_saturated` 只在账号维度为真 —— 它是给 S26 的提示，
    # 让它知道"换个池也没用"（§2.1 的 details）。
    assert svc.get_pool(T, video)["account"]["cap_inflight"] == 10


def test_t22_08_rpm_independent_of_inflight():
    """T22-08 inflight 有空位但 RPM 用尽 → 429 bottleneck=rpm，不占槽。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=1, rpm_remaining=1)
    _acquire(svc, _acquire_body("a1"), cid="c1")
    used, _ = _counts(svc, pid)
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, _acquire_body("a2"), cid="c2")
    assert exc.value.code == "NO_CAPACITY"
    assert exc.value.details["bottleneck"] == "rpm"
    assert exc.value.retryable is True
    # 被拒的那次没有占槽。
    assert _counts(svc, pid) == (used, used)


def test_t22_09_cache_degraded_refuses_then_recovers():
    """T22-09[E21] Redis 丢失 → 保守窗口内 503；重建后恢复；计数不变。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    _acquire(svc, _acquire_body("a1"), cid="c1")
    before = _counts(svc, pid)

    svc.mark_cache_degraded(seconds=60)
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, _acquire_body("a2"), cid="c2")
    assert exc.value.code == "CACHE_DEGRADED"
    assert exc.value.status == 503
    assert _counts(svc, pid) == before

    report = svc.rebuild_cache()
    assert report["reconcile"]["consistent"] is True
    assert svc.is_degraded() is False
    _acquire(svc, _acquire_body("a3"), cid="c3")
    assert _counts(svc, pid) == (before[0] + 1, before[1] + 1)


def test_t22_10_degraded_refuses_all_concurrent():
    """T22-10[E21] 清空 + 并发 200 → 全部在窗口内被拒；窗口后授予剩余容量。"""
    svc = _svc()
    pid = _pool(svc, limit=3, cap=3, rpm=None)
    svc.mark_cache_degraded(seconds=60)
    codes = []
    for i in range(200):
        try:
            _acquire(svc, _acquire_body(f"a{i}"), cid=f"c{i}")
        except ServiceError as exc:
            codes.append(exc.code)
    assert set(codes) == {"CACHE_DEGRADED"} and len(codes) == 200
    assert _counts(svc, pid) == (0, 0)

    svc.rebuild_cache()
    for i in range(10):
        try:
            _acquire(svc, _acquire_body(f"b{i}"), cid=f"cb{i}")
        except ServiceError as exc:
            assert exc.code == "NO_CAPACITY"
    assert _counts(svc, pid) == (3, 3)


def test_t22_11_restart_keeps_queue_and_inflight(tmp_path):
    """T22-11[E21] 进程重启 → 队列完整，INFLIGHT 计数不变。"""
    db = tmp_path / "q.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.executescript(SCHEMA)
    svc = SchedulerQuotaService(conn)
    pid = _pool(svc, limit=1, cap=1, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    for i in range(20):
        svc.enqueue(T, _acquire_body(f"w{i}"), correlation_id=f"q{i}")
    before = _counts(svc, pid)
    conn.close()

    conn2 = sqlite3.connect(db, isolation_level=None)
    svc2 = SchedulerQuotaService(conn2)
    assert _counts(svc2, pid) == before == (1, 1)
    queue = svc2.get_queue(T, pid)
    assert queue["count"] == 20


def test_t22_12_reconcile_after_restart_matches():
    """T22-12[E21] 重启 + Redis 丢失 → 对账 permit 聚合 == inflight_used。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    for i in range(3):
        _acquire(svc, _acquire_body(f"a{i}"), cid=f"c{i}")
    # 人为把投影写错，模拟恢复点不一致。
    svc.conn.execute("UPDATE quota_pool SET inflight_used = 99 WHERE pool_id = ?",
                     (pid,))
    report = svc.reconcile()
    assert report["consistent"] is False
    assert _counts(svc, pid) == (3, 3)
    assert svc.reconcile()["consistent"] is True


def test_t22_13_gpu_pool_uses_resource_seconds_only():
    """T22-13 gpu_second 池不占 account_cap_inflight，按资源秒计。"""
    svc = _svc()
    gpu = _pool(svc, limit=1, cap=1, resource_class="gpu_second",
                modality="video", rpm=None)
    body = _acquire_body("g1")
    body["resource_seconds_milli"] = 5000
    r = _acquire(svc, body, cid="cg1")
    pool = svc.get_pool(T, gpu)
    assert pool["inflight"]["used"] == 0
    assert pool["account"]["counted"] is False
    assert pool["resource_seconds"]["used_milli"] == 5000

    # 资源池的 permit 从未 dispatch，只能按 RESERVED 的合法理由释放。
    _release(svc, r["permit_id"], reason="NEVER_DISPATCHED")
    pool = svc.get_pool(T, gpu)
    assert pool["resource_seconds"]["used_milli"] == 0
    assert pool["inflight"]["used"] == 0


def test_t22_14_limit_downgrade_keeps_existing():
    """T22-14 limit 40→20 且 used=35：已有 35 个继续，新的被拒，降到 <20 后恢复。"""
    svc = _svc()
    pid = _pool(svc, limit=40, cap=40, rpm=None)
    permits = [_acquire(svc, _acquire_body(f"a{i}"), cid=f"c{i}") for i in range(35)]
    svc.set_pool_limits(T, pid, {"inflight_limit": 20},
                        actor={"service": "ops"}, correlation_id="lim")
    p = svc.get_pool(T, pid)
    assert p["inflight"]["limit"] == 20 and p["inflight"]["used"] == 35

    with pytest.raises(ServiceError) as exc:
        _acquire(svc, _acquire_body("new"), cid="cnew")
    assert exc.value.details["bottleneck"] == "inflight"

    # 放掉 16 个 → used=19 < 20，恢复授予。
    for r in permits[:16]:
        _release(svc, r["permit_id"], reason="NEVER_DISPATCHED")
    assert _counts(svc, pid) == (19, 19)
    _acquire(svc, _acquire_body("new"), cid="cnew2")
    assert _counts(svc, pid) == (20, 20)


def test_t22_15_config_time_sum_exceeds_cap_rejected():
    """T22-15 Σ pool.inflight_limit > account_cap → 422。"""
    svc = _svc()
    _pool(svc, limit=8, cap=10, modality="video")
    with pytest.raises(ServiceError) as exc:
        _pool(svc, limit=5, cap=10, modality="image")
    assert exc.value.status == 422
    assert exc.value.code == "SHARD_CAP_EXCEEDED"


# ============================================================ 7.3 Saga

def test_t22_16_no_permit_when_acquire_refused():
    """T22-16[E14] S22 acquire 429 → 无 permit、无计数变化。"""
    svc = _svc()
    pid = _pool(svc, limit=0 + 1, cap=1, rpm=None)
    _acquire(svc, _acquire_body("a1"), cid="c1")
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, _acquire_body("a2"), cid="c2")
    assert exc.value.code == "NO_CAPACITY"
    rows = svc.conn.execute("SELECT COUNT(*) AS n FROM permit").fetchone()["n"]
    assert rows == 1
    assert _counts(svc, pid) == (1, 1)


def test_t22_17_suspended_pool_returns_503():
    """T22-17[E14] 池 SUSPENDED → 503，无 permit。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    svc.suspend_pool(T, pid, {"suspend_reason": "ACCOUNT_REVOKED"},
                     actor={"service": "ops"}, correlation_id="s1")
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, _acquire_body("a1"), cid="c1")
    assert exc.value.status == 503
    assert exc.value.code == "POOL_SUSPENDED"
    assert exc.value.details["suspend_reason"] == "ACCOUNT_REVOKED"
    assert svc.conn.execute("SELECT COUNT(*) AS n FROM permit").fetchone()["n"] == 0


def test_t22_18_dispatch_retry_returns_same_dispatch_id():
    """T22-18[E15] confirm-dispatch 响应丢失后重试 → 同 dispatch_id，无第二个 permit。"""
    svc = _svc()
    _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"], dispatch_id="disp_x")
    again = _confirm_once(svc, r["permit_id"], r["grant_epoch"],
                          dispatch_id="disp_x", cid="d-retry")
    assert again["state"] == "INFLIGHT"
    assert again["dispatch_id"] == "disp_x"
    assert again["replayed"] is True
    assert svc.conn.execute("SELECT COUNT(*) AS n FROM permit").fetchone()["n"] == 1


def test_t22_19_stuck_at_dispatch_authorized():
    """T22-19[E15] 不推进 → permit 停在 DISPATCH_AUTHORIZED，且不可 TTL 回收。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1", ), cid="c1")
    # 只做第一次确认：S26 在等 S23 时停在这里。
    _confirm_once(svc, r["permit_id"], r["grant_epoch"])
    assert svc.get_permit(T, r["permit_id"])["state"] == "DISPATCH_AUTHORIZED"
    # 槽仍然占着（预占窗口不能被别人抢走）。
    assert _counts(svc, pid) == (1, 1)
    # janitor 不碰它。
    svc.conn.execute(
        "UPDATE permit SET lease_expires_at = '2020-01-01T00:00:00.000000Z'"
        " WHERE permit_id = ?", (r["permit_id"],))
    report = svc.sweep_expired()
    assert report["expired"] == []
    assert svc.get_permit(T, r["permit_id"])["state"] == "DISPATCH_AUTHORIZED"
    assert _counts(svc, pid) == (1, 1)


def test_t22_20_dispatch_on_released_is_409():
    """T22-20[E15] 已 RELEASED 再 confirm-dispatch → 409 PERMIT_NOT_DISPATCHABLE。"""
    svc = _svc()
    _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    _release(svc, r["permit_id"])
    with pytest.raises(ServiceError) as exc:
        _dispatch(svc, r["permit_id"], r["grant_epoch"] + 1)
    assert exc.value.code == "PERMIT_NOT_DISPATCHABLE"
    assert exc.value.status == 409


def test_t22_21_grant_epoch_conflict_is_409():
    """T22-21 `expected_grant_epoch` 被另一个执行器抢先 → 409，只有一个进入 INFLIGHT。

    真实的竞争形态：permit P 的授予序号是 1。执行器 A 用 1 推进成功；
    执行器 B 手里也是 1，但它要处理的其实是**更早那次授予**，此时 permit
    已经是 2（新 permit）——「旧 epoch 后到」就是 expected < 当前值。
    """
    svc = _svc()
    _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    epoch = r["grant_epoch"]
    _dispatch(svc, r["permit_id"], epoch)
    # 模拟另一个执行器在我们之后用同一 permit 拿到了更新的授予号。
    svc.conn.execute("UPDATE permit SET grant_epoch = ? WHERE permit_id = ?",
                     (epoch + 1, r["permit_id"]))
    with pytest.raises(ServiceError) as exc:
        _confirm_once(svc, r["permit_id"], epoch, cid="stale")
    assert exc.value.code == "GRANT_EPOCH_CONFLICT"
    assert exc.value.retryable is True
    assert (exc.value.details["expected_grant_epoch"],
            exc.value.details["current_grant_epoch"]) == (epoch, epoch + 1)
    # 状态与计数都没被后到者改动。
    assert svc.get_permit(T, r["permit_id"])["state"] == "INFLIGHT"


# ============================================================ 7.4 未知提交

def test_t22_22_unknown_submission_stays_inflight():
    """T22-22[E16] SUBMISSION_UNKNOWN → 保持 INFLIGHT，超时不自动释放。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    svc.consume_job_state_changed(
        {"id": "evt1", "tenant_id": T,
         "data": {"attempt_id": "a1", "state": "SUBMISSION_UNKNOWN"}},
        correlation_id="x")
    p = svc.get_permit(T, r["permit_id"])
    assert p["state"] == "INFLIGHT"
    assert p["reconcile_required_at"] is not None

    # 时间过去很久也不释放。
    svc.conn.execute(
        "UPDATE permit SET lease_expires_at = '2020-01-01T00:00:00.000000Z'"
        " WHERE permit_id = ?", (r["permit_id"],))
    assert svc.sweep_expired()["expired"] == []
    assert svc.get_permit(T, r["permit_id"])["state"] == "INFLIGHT"
    assert _counts(svc, pid) == (1, 1)


def test_t22_23_reconciled_absent_releases():
    """T22-23[E16] 供应商确认从未收到 → release(RECONCILED_ABSENT)。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    out = _release(svc, r["permit_id"], reason="RECONCILED_ABSENT")
    assert out["state"] == "RELEASED"
    assert out["release_reason"] == "RECONCILED_ABSENT"
    assert out["inflight_freed"] and out["account_freed"]
    assert _counts(svc, pid) == (0, 0)


def test_t22_24_unconfirmable_requires_operator_release():
    """T22-24[E16] 无法确认 → 永久 INFLIGHT；只能 OPERATOR 释放。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    # SETTLED 不是"确认没发出去"的证据，但仍是终态；这里验证 OPERATOR 路径可用。
    out = _release(svc, r["permit_id"], reason="OPERATOR",
                   evidence={"service_id": "S02", "id": "ticket_9"})
    assert out["state"] == "RELEASED"
    assert _counts(svc, pid) == (0, 0)
    ledger = svc.conn.execute(
        "SELECT reason FROM pool_usage_ledger WHERE permit_id = ? ORDER BY id",
        (r["permit_id"],)).fetchall()
    assert [x["reason"] for x in ledger] == ["ACQUIRE", "DISPATCH", "RELEASE"]


def test_t22_25_cancelled_job_does_not_release():
    """T22-25[E22] 任务取消 → permit 不释放；状态不为事件所动。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    svc.consume_job_state_changed(
        {"id": "evt2", "tenant_id": T,
         "data": {"attempt_id": "a1", "state": "CANCELLED"}},
        correlation_id="x")
    assert svc.get_permit(T, r["permit_id"])["state"] == "INFLIGHT"
    assert _counts(svc, pid) == (1, 1)


def test_t22_26_fence_bump_supersedes_undispatched_only():
    """T22-26[E22] fence 递增 → 未 dispatch 的释放；已 INFLIGHT 的不动。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    body_a = _acquire_body("a1", fence=1)
    body_a["run_id"] = "run_1"
    body_b = _acquire_body("a2", fence=1)
    body_b["run_id"] = "run_1"
    a = _acquire(svc, body_a, cid="c1")
    b = _acquire(svc, body_b, cid="c2")
    _dispatch(svc, b["permit_id"], b["grant_epoch"])

    out = svc.supersede_fence(
        T, {"run_id": "run_1", "execution_fence": 2},
        actor={"service": "S26"}, correlation_id="f2")
    assert out["released"] == [a["permit_id"]]
    assert out["inflight_untouched"] is True
    assert svc.get_permit(T, a["permit_id"])["state"] == "RELEASED"
    assert svc.get_permit(T, a["permit_id"])["release_reason"] == "SUPERSEDED_BY_FENCE"
    assert svc.get_permit(T, b["permit_id"])["state"] == "INFLIGHT"
    assert _counts(svc, pid) == (1, 1)


def test_t22_27_release_requires_evidence():
    """T22-27 release 缺 terminal_evidence_ref → 422；permit 不变。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    with pytest.raises(ServiceError) as exc:
        svc.release(T, r["permit_id"], {"reason": "SETTLED"},
                    actor={"service": "S26"}, correlation_id="rel")
    assert exc.value.status == 422
    assert svc.get_permit(T, r["permit_id"])["state"] == "INFLIGHT"
    assert _counts(svc, pid) == (1, 1)


def test_t22_28_and_29_repeated_release_idempotent():
    """T22-28/29 重复 release（同或不同 command_id）→ 首次结果，不重复扣减。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    first = _release(svc, r["permit_id"], cid="rel1")
    same = _release(svc, r["permit_id"], cid="rel1")
    other = _release(svc, r["permit_id"], cid="rel2")
    assert first["state"] == "RELEASED"
    assert same["replayed"] is True
    assert other["replayed"] is True
    assert other["inflight_freed"] is False
    assert _counts(svc, pid) == (0, 0)
    releases = svc.conn.execute(
        "SELECT COUNT(*) AS n FROM pool_usage_ledger WHERE permit_id = ?"
        " AND reason = 'RELEASE'", (r["permit_id"],)).fetchone()["n"]
    assert releases == 1


# ============================================================ 7.5 租约

def test_t22_30_expired_reserved_frees_both_slots():
    """T22-30 RESERVED 租约到期 → EXPIRED，两槽释放，事件发出。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    svc.conn.execute(
        "UPDATE permit SET lease_expires_at = '2020-01-01T00:00:00.000000Z'"
        " WHERE permit_id = ?", (r["permit_id"],))
    report = svc.sweep_expired()
    assert report["expired"] == [r["permit_id"]]
    assert svc.get_permit(T, r["permit_id"])["state"] == "EXPIRED"
    assert _counts(svc, pid) == (0, 0)
    sequences = [row["sequence"] for row in svc.conn.execute(
        "SELECT sequence FROM outbox WHERE aggregate_id = ? ORDER BY sequence",
        (r["permit_id"],)).fetchall()]
    assert sequences == [1, 5]


def test_t22_31_authorized_expiry_only_alerts():
    """T22-31 DISPATCH_AUTHORIZED 到期不 expire，只告警。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    # 正好停在 DISPATCH_AUTHORIZED（第一次确认之后就卡住等 S23）。
    _confirm_once(svc, r["permit_id"], r["grant_epoch"])
    svc.conn.execute(
        "UPDATE permit SET lease_expires_at = '2020-01-01T00:00:00.000000Z',"
        " updated_at = '2020-01-01T00:00:00.000000Z' WHERE permit_id = ?",
        (r["permit_id"],))
    report = svc.sweep_expired()
    assert report["expired"] == []
    assert report["authorized_stale_alerts"] is True
    assert report["authorized_stale_count"] == 1
    assert svc.get_permit(T, r["permit_id"])["state"] == "DISPATCH_AUTHORIZED"
    assert _counts(svc, pid) == (1, 1)


def test_t22_32_inflight_never_expires():
    """T22-32 INFLIGHT 永不释放。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    svc.conn.execute(
        "UPDATE permit SET lease_expires_at = '2000-01-01T00:00:00.000000Z'"
        " WHERE permit_id = ?", (r["permit_id"],))
    assert svc.sweep_expired()["expired"] == []
    with pytest.raises(ServiceError) as exc:
        svc.expire(r["permit_id"], actor={"service": "janitor"},
                   correlation_id="e")
    assert exc.value.code == "PERMIT_NOT_EXPIRABLE"
    assert _counts(svc, pid) == (1, 1)


def test_t22_33_concurrent_sweeps_no_double_processing(tmp_path):
    """T22-33 5 个 janitor 并发扫描 → 无重复处理。"""
    db = tmp_path / "q.db"
    conn = sqlite3.connect(db, isolation_level=None)
    conn.executescript(SCHEMA)
    svc = SchedulerQuotaService(conn)
    pid = _pool(svc, limit=10, cap=10, rpm=None)
    ids = [_acquire(svc, _acquire_body(f"a{i}"), cid=f"c{i}")["permit_id"]
           for i in range(10)]
    conn.execute("UPDATE permit SET lease_expires_at ="
                 " '2020-01-01T00:00:00.000000Z'")
    conn.close()

    results = []
    lock = threading.Lock()

    def worker():
        c = sqlite3.connect(db, isolation_level=None, timeout=30)
        s = SchedulerQuotaService(c)
        try:
            r = s.sweep_expired()
            with lock:
                results.append(r["expired"])
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    flat = [x for chunk in results for x in chunk]
    assert sorted(flat) == sorted(ids), "每个 permit 恰好被一个 janitor 处理"
    assert _counts(_svc(sqlite3.connect(db, isolation_level=None)), pid) == (0, 0)


def test_t22_34_janitor_failure_leaves_permit_reserved():
    """T22-34 janitor 中途失败 → 事务回滚，permit 仍 RESERVED。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    svc.conn.execute(
        "UPDATE permit SET lease_expires_at = '2020-01-01T00:00:00.000000Z'"
        " WHERE permit_id = ?", (r["permit_id"],))
    # 让 outbox 写入失败，触发 _expire_locked 内的回滚。
    svc.conn.execute(
        "CREATE TRIGGER boom BEFORE INSERT ON outbox BEGIN"
        " SELECT RAISE(ABORT, 'boom'); END")
    with pytest.raises(sqlite3.IntegrityError):
        svc.sweep_expired()
    svc.conn.execute("DROP TRIGGER boom")
    assert svc.get_permit(T, r["permit_id"])["state"] == "RESERVED"
    assert _counts(svc, pid) == (1, 1)
    # 修好后再扫一次仍然处理得到。
    assert svc.sweep_expired()["expired"] == [r["permit_id"]]


# ============================================================ 7.6 公平排序

def test_t22_35_guaranteed_share_protects_small_tenant():
    """T22-35 小租户不被饿死：guaranteed_share 生效。"""
    svc = _svc()
    pid = _pool(svc, limit=1, cap=1, rpm=None)
    svc.conn.execute(
        "INSERT INTO tenant_share (tenant_id, pool_id, weight, virtual_time_micro,"
        " inflight_granted, guaranteed_share, updated_at)"
        " VALUES (?, ?, 1, 0, 0, 1, '2020-01-01T00:00:00.000000Z')", (U, pid))

    # 大租户连续拿了 5 次，虚拟时间已经很超前。
    for i in range(5):
        r = _acquire(svc, _acquire_body(f"big{i}"), cid=f"big{i}")
        _release(svc, r["permit_id"], reason="NEVER_DISPATCHED")
        svc.conn.execute(
            "UPDATE tenant_share SET virtual_time_micro = 5000000"
            " WHERE tenant_id = ? AND pool_id = ?", (T, pid))

    big = svc.enqueue(T, _acquire_body("bigq"), correlation_id="q1")
    small = svc.enqueue(U, _acquire_body("smallq"), correlation_id="q2")
    head = svc.pick_next(pid)
    assert head["tenant_id"] == U, "保底份额未用完的租户优先"


def test_t22_36_virtual_time_alternates():
    """T22-36 同租户 100 个 vs 另一租户 1 个 → 虚拟时间使其交替。"""
    svc = _svc()
    pid = _pool(svc, limit=4, cap=4, rpm=None)
    # T 一口气排 100 个，U 排 1 个；两边虚拟时间都从 0 起。
    for i in range(100):
        svc.enqueue(T, _acquire_body(f"t{i}"), correlation_id=f"qt{i}")
    svc.enqueue(U, _acquire_body("u1"), correlation_id="qu")

    # 虚拟时间相同 → 按入队先后，T 的队首先被授予。
    assert svc.pick_next(pid)["tenant_id"] == T
    # T 被授予一格：虚拟时间前进。于是下一次是 U 的**唯一**那条，而不是
    # T 剩下的 99 条 —— 这就是「交替」，不靠优先级也不靠抢占。
    _acquire(svc, _acquire_body("t_grant"), cid="g1")
    assert svc.get_share(T, pool_id=pid)["virtual_time_micro"] > 0
    assert svc.pick_next(pid)["tenant_id"] == U
    assert svc.get_share(U, pool_id=pid)["virtual_time_micro"] == 0


def test_t22_37_priority_does_not_preempt_inflight():
    """T22-37 高优先级到达不影响已 INFLIGHT 的 permit。"""
    svc = _svc()
    pid = _pool(svc, limit=2, cap=2, rpm=None)
    running = _acquire(svc, _acquire_body("running"), cid="c1")
    _dispatch(svc, running["permit_id"], running["grant_epoch"])
    urgent = _acquire_body("urgent")
    urgent["priority"] = 0
    svc.enqueue(T, urgent, correlation_id="q")
    assert svc.get_permit(T, running["permit_id"])["state"] == "INFLIGHT"
    _release(svc, running["permit_id"], reason="SETTLED")


def test_t22_38_deadline_abandons():
    """T22-38 WAITING 超 deadline 或 queue_ttl → ABANDONED，不占计数。"""
    svc = _svc()
    pid = _pool(svc, limit=1, cap=1, rpm=None)
    body = _acquire_body("q1")
    body["deadline"] = "2020-01-01T00:00:00Z"
    entry = svc.enqueue(T, body, correlation_id="q")
    assert svc.pick_next(pid) is None
    row = svc.conn.execute(
        "SELECT state FROM queue_entry WHERE queue_entry_id = ?",
        (entry["queue_entry_id"],)).fetchone()
    assert row["state"] == "ABANDONED"
    assert _counts(svc, pid) == (0, 0)


# ============================================================ 7.7 安全

def test_t22_39_cross_tenant_permit_is_404():
    """T22-39[E28] 租户 B 读租户 A 的 permit → 404。"""
    svc = _svc()
    _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    with pytest.raises(ServiceError) as exc:
        svc.get_permit(U, r["permit_id"])
    assert exc.value.status == 404
    with pytest.raises(ServiceError):
        _release(svc, r["permit_id"], tid=U)


def test_t22_40_cross_tenant_pool_is_404():
    """T22-40[E28] 用别的租户的池 → 404，不泄漏存在性。"""
    svc = _svc()
    byok = _pool(svc, limit=5, cap=5, rpm=None, tid=T, scope=T,
                 account="acct_byok")
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    with pytest.raises(ServiceError) as exc:
        svc.get_pool(U, byok)
    assert exc.value.status == 404
    with pytest.raises(ServiceError) as exc:
        svc.get_permit(U, r["permit_id"])
    assert exc.value.status == 404
    assert "acct_byok" not in str(exc.value.details)


def test_t22_42_byok_pool_not_borrowable():
    """T22-42 BYOK 池被别的租户请求 → 404（不能借凭证容灾）。"""
    svc = _svc()
    byok = _pool(svc, limit=5, cap=5, rpm=None, tid=T, scope=T,
                 account="acct_byok")
    body = _acquire_body("u1", pool_id=byok)
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, body, tid=U)
    assert exc.value.status == 404
    assert svc.conn.execute("SELECT COUNT(*) AS n FROM permit").fetchone()["n"] == 0


# ============================================================ 7.8 供应商异常

def test_t22_45_no_pool_for_route_does_not_search():
    """T22-45[E18] 池不存在 → 422；不搜索其他池、不建 permit、不占计数。"""
    svc = _svc()
    _pool(svc, limit=5, cap=5, rpm=None, modality="video")
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, _acquire_body("a1", modality="speech"), cid="c1")
    assert exc.value.code == "POOL_NOT_FOUND_FOR_ROUTE"
    assert exc.value.status == 422
    assert svc.conn.execute("SELECT COUNT(*) AS n FROM permit").fetchone()["n"] == 0
    assert svc.conn.execute(
        "SELECT SUM(inflight_used) AS n FROM quota_pool").fetchone()["n"] == 0


def test_t22_46_suspended_pool_not_borrowed():
    """T22-46[E18] 唯一匹配池 SUSPENDED → 503；不借用同 provider 的其他账号。

    路由钉了 `account_ref`（S25 的 RouteDecision 含 `account_pool`，见 docs/09
    的模型主键）时，S22 只能在那一个账号里找池。同 provider 的另一个健康账号
    存在也不行 —— 换账号是 S25 的 L1 降级，必须由 S25 重新决策。
    """
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None, account="acct1")
    # 同 provider、同 channel、同 region、同 modality，但**另一个账号**，且有余量。
    other = _pool(svc, limit=5, cap=5, rpm=None, account="acct2")
    svc.suspend_pool(T, pid, {"suspend_reason": "ACCOUNT_REVOKED"},
                     actor={"service": "ops"}, correlation_id="s")
    body = _acquire_body("a1")
    body["account_ref"] = "acct1"
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, body, cid="c1")
    assert exc.value.status == 503
    assert exc.value.code == "POOL_SUSPENDED"
    assert exc.value.details["suspend_reason"] == "ACCOUNT_REVOKED"
    # 另一个账号的池没有被偷偷用掉。
    assert svc.get_pool(T, other)["inflight"]["used"] == 0


def test_t22_46c_unpinned_route_balances_across_healthy_pools():
    """路由**没**钉账号时，S22 在同一路由的健康池之间分摊 —— 这不是降级。

    路由元组四位（provider/channel/region/modality）相同，账号那一格由 S22 选。
    S25 的 L1 是"换另一个健康账户池"，前提是它自己做出了那个决策；没做出
    决策时（路由未钉账号）在健康池间做负载均衡本来就是 S22 的职责。
    """
    svc = _svc()
    a = _pool(svc, limit=5, cap=10, rpm=None, account="acct1")
    b = _pool(svc, limit=5, cap=10, rpm=None, account="acct2")
    seen = set()
    for i in range(2):
        seen.add(_acquire(svc, _acquire_body(f"a{i}"), cid=f"c{i}")["pool_id"])
    assert seen == {a, b}, "负载分摊应该用上两个池"


def test_t22_46b_existing_permits_survive_suspension():
    """T22-46 后半：已有 permit 不受 SUSPENDED 影响，仍可正常释放。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    svc.suspend_pool(T, pid, {"suspend_reason": "DRIFT"},
                     actor={"service": "ops"}, correlation_id="s")
    with pytest.raises(ServiceError):
        _acquire(svc, _acquire_body("a2"), cid="c2")
    _release(svc, r["permit_id"], reason="SETTLED")
    assert _counts(svc, pid) == (0, 0)


def test_t22_47_rpm_refusal_rolls_back_counter():
    """T22-47[E18] RPM 用尽 → 429 bottleneck=rpm，回滚后 inflight_used 不变。"""
    svc = _svc()
    pid = _pool(svc, limit=10, cap=10, rpm=2, rpm_remaining=2)
    _acquire(svc, _acquire_body("a1"), cid="c1")
    _acquire(svc, _acquire_body("a2"), cid="c2")
    before = _counts(svc, pid)
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, _acquire_body("a3"), cid="c3")
    assert exc.value.details["bottleneck"] == "rpm"
    assert _counts(svc, pid) == before


def test_t22_48_modality_mismatch_not_degraded():
    """T22-48[E18] modality 不符 → 422，不静默降到低画质/便宜模型。"""
    svc = _svc()
    low = _pool(svc, limit=5, cap=5, rpm=None, modality="video",
                account="low_q")
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, _acquire_body("a1", pool_id=low, modality="image"), cid="c1")
    assert exc.value.code == "POOL_MODALITY_MISMATCH"
    assert svc.get_pool(T, low)["inflight"]["used"] == 0


def test_t22_49_resume_requires_authorization():
    """T22-49[E18] resume 需 S02 授权；之后 acquire 恢复。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    svc.suspend_pool(T, pid, {"suspend_reason": "5XX"},
                     actor={"service": "ops"}, correlation_id="s")
    with pytest.raises(ServiceError) as exc:
        svc.resume_pool(T, pid, {}, actor={"service": "ops"},
                        correlation_id="r1")
    assert exc.value.status == 403
    svc.resume_pool(T, pid, {"authorization_ref": "s02_auth_1"},
                    actor={"service": "S02"}, correlation_id="r2")
    assert svc.get_pool(T, pid)["state"] == "ACTIVE"
    _acquire(svc, _acquire_body("a1"), cid="c1")


def test_t22_50_stale_pool_definition_discarded():
    """T22-50[E18] 池定义更旧的 revision → 丢弃，池不倒退。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    rev = svc.get_pool(T, pid)["revision"]
    applied = svc.consume_model_capability_changed(
        {"id": "v2", "data": {"pool_id": pid, "revision": rev + 1,
                              "inflight_limit": 9}},
        correlation_id="m")
    assert applied["applied"] is True
    stale = svc.consume_model_capability_changed(
        {"id": "v1", "data": {"pool_id": pid, "revision": rev,
                              "inflight_limit": 1}},
        correlation_id="m")
    assert stale["applied"] is False
    assert svc.get_pool(T, pid)["inflight"]["limit"] == 9


def test_t22_51_no_pool_switching_branch():
    """T22-51[E18] 静态断言：acquire 路径上不存在换池重试。

    降级决策只能在 S25。任何"这个池不行，试试别的池"的循环都会让 S22
    变成第二路由权威（§0.1）。

    断言范围限定在 acquire 的调用链（`_acquire_once` → `_resolve_pool` →
    `_check_capacity`）——`reconcile()` 里有一处只读的逐池对账扫描，那是
    §5.5 第 3 步，不是选池。
    """
    source = (ROOT / "src" / "scheduler_quota" / "service.py").read_text(
        encoding="utf-8")
    start = source.index("    def _acquire_once(")
    end = source.index("    def _acquire_waiting(")
    acquire_path = source[start:end].lower()
    for forbidden in ("for pool in pools", "try the next pool", "fallback_pool",
                      "alternative_pool", "next_provider", "retry_other_account"):
        assert forbidden not in acquire_path, \
            f"found degradation branch in acquire path: {forbidden}"

    # 选池只在同一路由元组内挑候选，且路由钉了账号就再收窄一层。
    resolve = source[source.index("    def _resolve_pool("):
                     source.index("    # -------------------------------------------------------------- 容量")]
    assert "provider_id = ? AND channel_id = ?" in resolve
    assert " AND account_ref = ?" in resolve
    # 越界字段一个都不许出现：这三个正是"跨区域/跨 provider 借"的写法。
    for field in ("provider_id !=", "region !=", "OR account_ref"):
        assert field not in resolve, f"pool resolution widens its own route: {field}"


# ============================================================ 补充：事件/契约

def test_permit_changed_event_shape():
    """§6.1：data 恰好 13 个字段，aggregate_sequence 是状态编码。"""
    import json
    svc = _svc()
    _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    payload = json.loads(svc.conn.execute(
        "SELECT payload FROM outbox WHERE aggregate_id = ?",
        (r["permit_id"],)).fetchone()["payload"])
    assert set(payload["data"]) == {
        "schema_version", "permit_id", "pool_id", "attempt_id",
        "previous_state", "state", "release_reason", "dispatch_id",
        "execution_fence", "inflight_used", "inflight_limit",
        "account_used_inflight", "account_cap_inflight",
    }
    assert payload["data"]["schema_version"] == "2.0"
    assert payload["sequence"] == 1
    assert payload["causation_id"]


def test_access_revocation_cancels_only_waiting():
    """§6.2 access.grant.changed → 只取消 WAITING，已授予 permit 不动。"""
    svc = _svc()
    pid = _pool(svc, limit=1, cap=1, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    e1 = svc.enqueue(U, _acquire_body("w1"), correlation_id="q1")
    e2 = svc.enqueue(U, _acquire_body("w2"), correlation_id="q2")
    out = svc.consume_access_revoked(
        {"id": "ev", "data": {"tenant_id": U}}, correlation_id="x")
    assert out["cancelled_count"] == 2
    states = [row["state"] for row in svc.conn.execute(
        "SELECT state FROM queue_entry WHERE queue_entry_id IN (?, ?)", (e1["queue_entry_id"], e2["queue_entry_id"]))]
    assert set(states) == {"CANCELLED"}
    assert svc.get_permit(T, r["permit_id"])["state"] == "INFLIGHT"


def test_observation_backfills_job_ref_only():
    """§6.2 connector.observation.received → 只回填 provider_job_ref。"""
    svc = _svc()
    _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    svc.consume_observation_received(
        {"id": "obs1", "data": {"attempt_id": "a1", "provider_job_ref": "job_9"}},
        correlation_id="x")
    p = svc.get_permit(T, r["permit_id"])
    assert p["provider_job_ref"] == "job_9"
    assert p["state"] == "INFLIGHT"


def test_job_state_event_never_advances_permit():
    """§6.2 S22 绝不因 job SUCCEEDED 释放 permit。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    _dispatch(svc, r["permit_id"], r["grant_epoch"])
    out = svc.consume_job_state_changed(
        {"id": "ev3", "data": {"attempt_id": "a1", "state": "SUCCEEDED"}},
        correlation_id="x")
    assert out["marked"] == []
    assert svc.get_permit(T, r["permit_id"])["state"] == "INFLIGHT"
    assert _counts(svc, pid) == (1, 1)


def test_ledger_is_append_only():
    """§1.4 计数流水不可改、不可删。"""
    svc = _svc()
    _pool(svc, limit=5, cap=5, rpm=None)
    _acquire(svc, _acquire_body("a1"), cid="c1")
    with pytest.raises(sqlite3.IntegrityError):
        svc.conn.execute("DELETE FROM pool_usage_ledger")
    with pytest.raises(sqlite3.IntegrityError):
        svc.conn.execute("UPDATE pool_usage_ledger SET delta_inflight = 0")


def test_acquire_requires_reservation_ref_and_amount():
    """§2.7 INVALID_RESERVATION_REF。"""
    svc = _svc()
    _pool(svc, limit=5, cap=5, rpm=None)
    bad = _acquire_body("a1")
    bad["reservation_ref"] = ""
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, bad, cid="c1")
    assert exc.value.code == "INVALID_RESERVATION_REF"
    neg = _acquire_body("a2", amount=-1)
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, neg, cid="c2")
    assert exc.value.code == "INVALID_RESERVATION_REF"


def test_fence_superseded_rejects_stale_acquire():
    """§2.7 FENCE_SUPERSEDED。

    fence 只在**同一个 attempt** 内比较：换了 attempt 就是另一条执行线，
    它有自己的 fence 序列。同 attempt 下用更小的 fence 重新领取，说明上一轮
    已经被更新的 fence 接手，这次领取就是陈旧的。
    """
    svc = _svc()
    _pool(svc, limit=5, cap=5, rpm=None)
    body = _acquire_body("same", fence=3)
    body["deductible_key"] = "dk1"
    _acquire(svc, body, cid="c1")
    # 换一个 deductible_key，否则先撞的是幂等回放（那句也没错，只是不是这条）。
    body2 = _acquire_body("same", fence=2)
    body2["deductible_key"] = "dk2"
    with pytest.raises(ServiceError) as exc:
        _acquire(svc, body2, cid="c2")
    assert exc.value.code == "FENCE_SUPERSEDED"
    assert exc.value.details == {"attempt_id": "same", "execution_fence": 2,
                                 "current_fence": 3}


def test_expire_rejects_cross_tenant():
    """§2.4 内部端点仍不得跨租户。"""
    svc = _svc()
    _pool(svc, limit=5, cap=5, rpm=None)
    r = _acquire(svc, _acquire_body("a1"), cid="c1")
    with pytest.raises(ServiceError) as exc:
        svc.expire(r["permit_id"], actor={"service": "janitor"},
                   correlation_id="e", tenant_id=U)
    assert exc.value.status == 404


def test_queue_snapshot_hides_other_tenants():
    """§2.5 队列快照：别的租户只给位置，不给请求标识。"""
    svc = _svc()
    pid = _pool(svc, limit=1, cap=1, rpm=None)
    svc.enqueue(T, _acquire_body("t1"), correlation_id="q1")
    svc.enqueue(U, _acquire_body("u1"), correlation_id="q2")
    snap = svc.get_queue(T, pid)
    mine = [i for i in snap["items"] if i["tenant_id"] == T]
    other = [i for i in snap["items"] if i["tenant_id"] == "other"]
    assert len(mine) == 1 and len(other) == 1
    assert other[0]["queue_entry_id"] is None


def test_pool_resume_rejects_without_authorization():
    """§3.3 resume 必须带 authorization_ref。"""
    svc = _svc()
    pid = _pool(svc, limit=5, cap=5, rpm=None)
    svc.suspend_pool(T, pid, {"suspend_reason": "x"},
                     actor={"service": "ops"}, correlation_id="s")
    with pytest.raises(ServiceError) as exc:
        svc.resume_pool(T, pid, {}, actor={"service": "ops"},
                        correlation_id="r")
    assert exc.value.status == 403
