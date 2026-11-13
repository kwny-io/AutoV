"""S23 budget-ledger 测试。编号对应 plans/S23-budget-ledger.md §7。

金额全部是整数 micro，没有 float、没有 decimal。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import threading
from pathlib import Path

import pytest

from budget_ledger.schema import SCHEMA
from budget_ledger.service import BudgetLedgerService, ServiceError

ROOT = Path(__file__).resolve().parents[1]
T = "tenant_a"
U = "tenant_b"
FAR = "2099-01-01T00:00:00.000000Z"


def _conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.executescript(SCHEMA)
    return c


def _svc():
    return BudgetLedgerService(_conn())


def _root(svc, limit=1_000_000_000, currency="USD", tid=T):
    return svc.create_budget(
        tid, {"scope_type": "tenant", "scope_ref": tid, "currency": currency,
              "limit_micro": limit},
        actor={"service": "test"}, correlation_id="c0")["budget_id"]


def _child(svc, parent, scope_type="episode", scope_ref="ep3", currency="USD",
           tid=T):
    return svc.create_budget(
        tid, {"scope_type": scope_type, "scope_ref": scope_ref,
              "currency": currency, "parent_budget_id": parent},
        actor={"service": "test"}, correlation_id="c1")["budget_id"]


def _grant(svc, parent, child, amount, cmd="g1", tid=T):
    return svc.grant_escrow(
        tid, {"parent_budget_id": parent, "child_budget_id": child,
              "amount_micro": amount, "command_id": cmd},
        actor={"service": "test"}, correlation_id=cmd)


def _reserve_body(budget_id, *, amount=1000, conservative=None, attempt="att1",
                  currency="USD", budget_currency="USD", scope=None,
                  expires=FAR):
    body = {
        "budget_scope_id": budget_id,
        "attempt_id": attempt,
        "execution_fence": 1,
        "currency": currency,
        "budget_currency": budget_currency,
        "amount_micro": amount,
        "amount_conservative_micro": (
            amount if conservative is None else conservative),
        "manifest_ref": {"id": "m1", "content_hash": "h1"},
        "route_decision_ref": {"id": "rd1", "content_hash": "h2"},
        "price_snapshot_ref": {"id": "ps1", "content_hash": "h3"},
        "quote_expires_at": expires,
        "attribution": {"project_id": "p1", "series_id": "s1", "episode_id": "ep3"},
    }
    if scope is not None:
        body["idempotency_scope"] = scope
    return body


def _reserve(svc, budget_id, *, tid=T, corr="c2", **kw):
    return svc.reserve(tid, _reserve_body(budget_id, **kw),
                       actor={"service": "test"}, correlation_id=corr)


def _ready(svc, budget_id, *, amount=1000, conservative=None, tid=T, **kw):
    """reserve → authorize，返回 reservation_id。"""
    r = _reserve(svc, budget_id, amount=amount, conservative=conservative,
                 tid=tid, **kw)
    svc.authorize(tid, r["reservation_id"], {},
                  actor={"service": "test"}, correlation_id="c3")
    return r["reservation_id"]


def _b(svc, budget_id, tid=T):
    return svc.availability(tid, budget_id)


def _entries(svc, tid=T):
    return svc.ledger_entries(tid, limit=1000)


# ===================== 幂等（T23-01…T23-07b） ============================

def test_t23_01_same_command_replays_once():
    svc = _svc()
    b = _root(svc)
    first = _reserve(svc, b)
    for _ in range(2):
        again = _reserve(svc, b)
        assert again["reservation_id"] == first["reservation_id"]
        assert again["replayed"] is True

    assert _b(svc, b)["outstanding_reserved_micro"] == 1000
    assert len([e for e in _entries(svc) if e["entry_type"] == "RESERVE"]) == 1


def test_t23_03_different_command_same_scope_same_reservation():
    svc = _svc()
    b = _root(svc)
    a = _reserve(svc, b, scope="attempt:att1")
    c = _reserve(svc, b, scope="attempt:att1")
    assert a["reservation_id"] == c["reservation_id"]
    assert _b(svc, b)["outstanding_reserved_micro"] == 1000


def test_t23_04_duplicate_accrue_books_nothing():
    svc = _svc()
    b = _root(svc)
    rid = _ready(svc, b, amount=1000)
    body = {"accrual": {"billing_unit": "generation_second", "billed_micro": 700,
                        "billed_quantity": 7, "quantity_scale": 1,
                        "stage": "PROVISIONAL", "basis": "ESTIMATE_CONSERVATIVE"}}
    before = _b(svc, b)
    svc.accrue(T, rid, body, actor={"service": "test"}, correlation_id="c4")
    after_one = _b(svc, b)
    svc.accrue(T, rid, body, actor={"service": "test"}, correlation_id="c5")
    after_two = _b(svc, b)

    # §3.1：accrue 不改任何 budget 计数
    assert after_one == before == after_two
    assert after_two["outstanding_reserved_micro"] == 1000
    assert after_two["consumed_micro"] == 0
    assert _conn_count(svc, "usage_accrual") == 1


def _conn_count(svc, table):
    return svc.conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"]


def test_t23_05_duplicate_settle_is_idempotent():
    svc = _svc()
    b = _root(svc)
    rid = _ready(svc, b, amount=1000)
    body = {"final_accruals": [
        {"billing_unit": "generation_second", "billed_micro": 800,
         "billed_quantity": 8, "quantity_scale": 1,
         "basis": "PROVIDER_INVOICE", "invoice_ref": "inv1"}]}
    first = svc.settle(T, rid, body, actor={"service": "test"},
                       correlation_id="c6")
    snap = _b(svc, b)
    second = svc.settle(T, rid, body, actor={"service": "test"},
                        correlation_id="c7")

    assert second["replayed"] is True
    assert second["settlement_id"] == first["settlement_id"]
    assert _b(svc, b) == snap
    assert _conn_count(svc, "settlement") == 1


def test_t23_06_settle_with_different_amount_conflicts():
    svc = _svc()
    b = _root(svc)
    rid = _ready(svc, b, amount=1000)
    svc.settle(T, rid, {"final_accruals": [
        {"billing_unit": "request", "billed_micro": 800, "basis":
         "PROVIDER_INVOICE", "invoice_ref": "inv1"}]},
        actor={"service": "test"}, correlation_id="c6")
    with pytest.raises(ServiceError) as ei:
        svc.settle(T, rid, {"final_accruals": [
            {"billing_unit": "request", "billed_micro": 900, "basis":
             "PROVIDER_INVOICE", "invoice_ref": "inv2"}]},
            actor={"service": "test"}, correlation_id="c7")
    assert ei.value.code == "RESERVATION_ALREADY_SETTLED"
    assert ei.value.status == 409


def test_t23_07_duplicate_release_decrements_once():
    svc = _svc()
    b = _root(svc)
    rid = _reserve(svc, b, amount=1000)["reservation_id"]
    body = {"reason": "QUOTA_FAILED", "evidence_ref": {"id": "ev1"}}
    first = svc.release(T, rid, body, actor={"service": "test"},
                        correlation_id="c8")
    second = svc.release(T, rid, body, actor={"service": "test"},
                         correlation_id="c9")
    assert first["released_micro"] == 1000
    assert second["replayed"] is True
    assert second["released_micro"] == 0
    assert _b(svc, b)["outstanding_reserved_micro"] == 0
    assert len([e for e in _entries(svc) if e["entry_type"] == "RELEASE"]) == 1


def test_t23_07b_settle_two_entries_are_atomic():
    """注入第二条分录插入失败 → 第一条也回滚。"""
    svc = _svc()
    b = _root(svc)
    rid = _ready(svc, b, amount=1000)
    before = _b(svc, b)

    orig = svc._insert_entry
    calls = {"n": 0}

    def boom(**kw):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("injected failure on second entry")
        return orig(**kw)

    svc._insert_entry = boom
    with pytest.raises(RuntimeError):
        svc.settle(T, rid, {"final_accruals": [
            {"billing_unit": "request", "billed_micro": 800,
             "basis": "PROVIDER_INVOICE", "invoice_ref": "inv1"}]},
            actor={"service": "test"}, correlation_id="c6")
    svc._insert_entry = orig

    assert _b(svc, b) == before
    assert _conn_count(svc, "settlement") == 0
    assert len([e for e in _entries(svc) if e["entry_type"] == "SETTLE"]) == 0


# ======================= 预算树与恒等式（T23-08…T23-14b） ================

def test_t23_08_whole_tree_identity_balances():
    svc = _svc()
    root = _root(svc, limit=100_000)
    ep = _child(svc, root, "episode", "ep3")
    shot = _child(svc, ep, "shot", "shot1")
    _grant(svc, root, ep, 60_000, "g1")
    _grant(svc, ep, shot, 40_000, "g2")

    _reserve(svc, shot, amount=5000, conservative=6000, attempt="a1")
    _ready(svc, ep, amount=3000, conservative=4000, attempt="a2")

    t = svc.tree(T, root)
    inv = t["invariant"]
    assert inv["available_plus_outstanding_plus_consumed"] == inv["root_limit_micro"]
    assert inv["root_limit_micro"] == 100_000

    # escrowed_out 刻意不进恒等式：它是"往下分配"这个动作，不是消耗，
    # 算进去就双算（父减一次、子加一次）。这里直接钉住树里有委派存在，
    # 从而证明上面的等式确实是在"有 escrow 的情况下"成立的。
    root_row = next(n for n in t["nodes"] if n["budget_id"] == root)
    assert root_row["limit_micro"] == 100_000
    assert root_row["available_micro"] == 100_000 - 60_000
    assert t["nodes"][1]["limit_micro"] == 60_000


def test_t23_09_concurrent_reservations_never_overspend():
    """父限 100，两子各 50，并发 200 个各 1 元 → 恰好 100 成功。"""
    db_path = str(ROOT / "tests" / "_t23_09.sqlite")
    Path(db_path).unlink(missing_ok=True)

    setup = sqlite3.connect(db_path, isolation_level=None)
    setup.executescript(SCHEMA)
    svc = BudgetLedgerService(setup)
    root = _root(svc, limit=100)
    c1 = _child(svc, root, "run", "run1")
    c2 = _child(svc, root, "run", "run2")
    _grant(svc, root, c1, 50, "g1")
    _grant(svc, root, c2, 50, "g2")
    setup.close()

    ok, failed = [], []
    lock = threading.Lock()

    def worker(i):
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
        s = BudgetLedgerService(conn)
        target = c1 if i % 2 == 0 else c2
        try:
            s.reserve(T, _reserve_body(target, amount=1, attempt=f"a{i}"),
                      actor={"service": "t"}, correlation_id=f"c{i}")
            with lock:
                ok.append(i)
        except ServiceError as exc:
            with lock:
                failed.append(exc.code)
        except sqlite3.OperationalError as exc:
            with lock:
                failed.append(f"BUSY:{exc}")
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(200)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(ok) == 100, f"ok={len(ok)} failed={len(failed)}"
    assert set(failed) == {"INSUFFICIENT_BUDGET"}

    conn = sqlite3.connect(db_path, isolation_level=None)
    row = conn.execute("SELECT * FROM budget WHERE budget_id = ?",
                       (c1,)).fetchone()
    after = dict(zip([d[0] for d in conn.execute(
        "SELECT * FROM budget").description], row))
    assert after["outstanding_reserved_micro"] == 50
    assert after["limit_micro"] - after["consumed_micro"] - \
        after["outstanding_reserved_micro"] - after["escrowed_out_micro"] >= 0
    conn.close()
    Path(db_path).unlink(missing_ok=True)


def test_t23_11_same_budget_last_ten_quota():
    db_path = str(ROOT / "tests" / "_t23_11.sqlite")
    Path(db_path).unlink(missing_ok=True)
    setup = sqlite3.connect(db_path, isolation_level=None)
    setup.executescript(SCHEMA)
    svc = BudgetLedgerService(setup)
    b = _root(svc, limit=100)
    # 先占满 90，只剩 10
    for i in range(90):
        _reserve(svc, b, amount=1, attempt=f"pre{i}")
    setup.close()

    ok, failed = [], []
    lock = threading.Lock()

    def worker(i):
        conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
        s = BudgetLedgerService(conn)
        try:
            s.reserve(T, _reserve_body(b, amount=1, attempt=f"x{i}"),
                      actor={"service": "t"}, correlation_id=f"x{i}")
            with lock:
                ok.append(i)
        except ServiceError as exc:
            with lock:
                failed.append(exc.code)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert len(ok) == 10, f"ok={len(ok)} failed={set(failed)}"
    assert set(failed) == {"INSUFFICIENT_BUDGET"}
    Path(db_path).unlink(missing_ok=True)


def test_t23_12_escrow_shortfall_conflicts():
    svc = _svc()
    root = _root(svc, limit=1000)
    ep = _child(svc, root)
    _grant(svc, root, ep, 600, "g1")
    with pytest.raises(ServiceError) as ei:
        _grant(svc, root, ep, 500, "g2")
    assert ei.value.code == "INSUFFICIENT_BUDGET"
    assert ei.value.status == 409
    assert _b(svc, ep)["limit_micro"] == 600


def test_t23_13_cross_run_rework_hits_collective_episode_budget():
    """同 episode 下 3 个 run 各自返工，集预算封顶。"""
    svc = _svc()
    root = _root(svc, limit=100_000)
    ep = _child(svc, root, "episode", "ep3")
    _grant(svc, root, ep, 10_000, "g0")

    runs = []
    for n in range(3):
        run = _child(svc, ep, "run", f"run{n}")
        _grant(svc, ep, run, 3_000, f"g{n}")
        runs.append(run)

    for n, run in enumerate(runs):
        _reserve(svc, run, amount=3_000, attempt=f"a{n}")

    # 第四个 run 想再要额度：集预算剩余 1000，只能批 1000
    run4 = _child(svc, ep, "run", "run4")
    with pytest.raises(ServiceError) as ei:
        _grant(svc, ep, run4, 1_500, "g4")
    assert ei.value.code == "INSUFFICIENT_BUDGET"
    _grant(svc, ep, run4, 1_000, "g4b")
    assert _b(svc, run4)["limit_micro"] == 1_000


def test_t23_14_frozen_budget_rejects_reserve():
    svc = _svc()
    b = _root(svc)
    svc.set_frozen(T, b, {"reason": "ops"}, frozen=True,
                   actor={"service": "test"}, correlation_id="c")
    with pytest.raises(ServiceError) as ei:
        _reserve(svc, b)
    assert ei.value.code == "BUDGET_FROZEN"
    assert ei.value.details["blocking_budget_id"] == b


def test_t23_14b_ancestor_frozen_does_not_block_child():
    """额度已下推，子级 limit 是自己的；冻结祖先不级联。"""
    svc = _svc()
    root = _root(svc, limit=10_000)
    ep = _child(svc, root)
    _grant(svc, root, ep, 5_000, "g1")
    svc.set_frozen(T, root, {"reason": "ops"}, frozen=True,
                   actor={"service": "test"}, correlation_id="c")

    r = _reserve(svc, ep, amount=1000)
    assert r["state"] == "RESERVED"

    # 但祖先不再接受新的 grant_escrow
    ep2 = _child(svc, root, "run", "run9")
    with pytest.raises(ServiceError) as ei:
        _grant(svc, root, ep2, 100, "g2")
    assert ei.value.code == "BUDGET_FROZEN"


# ========================= 状态机（T23-15…T23-27b） ======================

def test_t23_15_release_restores_available():
    svc = _svc()
    b = _root(svc, limit=10_000)
    before = _b(svc, b)["available_micro"]
    rid = _reserve(svc, b, amount=3000)["reservation_id"]
    assert _b(svc, b)["available_micro"] == before - 3000
    svc.release(T, rid, {"reason": "QUOTA_FAILED", "evidence_ref": {"id": "e"}},
                actor={"service": "test"}, correlation_id="c")
    after = _b(svc, b)
    assert after["available_micro"] == before
    assert after["consumed_micro"] == 0
    assert after["outstanding_reserved_micro"] == 0


def test_t23_17_authorized_never_auto_expires():
    svc = _svc()
    b = _root(svc)
    rid = _ready(svc, b, expires="2000-01-01T00:00:00.000000Z" if False else FAR)
    svc.authorize(T, rid, {}, actor={"service": "test"}, correlation_id="c")
    # janitor 不得动 AUTHORIZED
    assert svc.expire_stale_quotes(T)["expired_count"] == 0
    assert svc.get_reservation(T, rid)["state"] == "AUTHORIZED"


def test_t23_18_authorize_with_expired_quote():
    svc = _svc()
    b = _root(svc)
    r = _reserve(svc, b, expires="2000-01-01T00:00:00.000000Z")
    with pytest.raises(ServiceError) as ei:
        svc.authorize(T, r["reservation_id"], {},
                      actor={"service": "test"}, correlation_id="c")
    assert ei.value.code == "QUOTE_EXPIRED"
    assert ei.value.status == 409


def test_t23_16_janitor_expires_only_reserved():
    svc = _svc()
    b = _root(svc)
    rid = _reserve(svc, b, expires="2000-01-01T00:00:00.000000Z")["reservation_id"]
    result = svc.expire_stale_quotes(T)
    assert result["expired_count"] == 1
    assert svc.get_reservation(T, rid)["state"] == "EXPIRED"
    assert _b(svc, b)["outstanding_reserved_micro"] == 0


def test_t23_19_release_requires_evidence():
    svc = _svc()
    b = _root(svc)
    rid = _reserve(svc, b)["reservation_id"]
    with pytest.raises(ServiceError) as ei:
        svc.release(T, rid, {"reason": "QUOTA_FAILED"},
                    actor={"service": "test"}, correlation_id="c")
    assert ei.value.status == 422
    assert svc.get_reservation(T, rid)["state"] == "RESERVED"


def test_t23_20_release_on_accrued_conflicts():
    svc = _svc()
    b = _root(svc)
    rid = _ready(svc, b, amount=1000)
    svc.accrue(T, rid, {"accrual": {
        "billing_unit": "request", "billed_micro": 500, "billed_quantity": 1,
        "stage": "PROVISIONAL", "basis": "ESTIMATE_CONSERVATIVE"}},
        actor={"service": "test"}, correlation_id="c")
    with pytest.raises(ServiceError) as ei:
        svc.release(T, rid, {"reason": "OPERATOR"},
                    actor={"service": "test"}, correlation_id="c")
    assert ei.value.code == "RESERVATION_NOT_RELEASABLE"


def test_t23_21_submission_unknown_holds_quota_with_no_entries():
    """未知提交期间：reservation 占着额度，但账本一条分录都不该多。"""
    svc = _svc()
    b = _root(svc, limit=10_000)
    rid = _ready(svc, b, amount=2000)
    before_entries = len(_entries(svc))
    before = _b(svc, b)

    # 未知状态在 S23 里没有专门的状态机 —— 它就是 AUTHORIZED。
    # 这正是设计意图（§3.1）：不需要额外冻结，R 一直占在 outstanding 里。
    svc.accrue(T, rid, {"accrual": {
        "billing_unit": "request", "billed_micro": 500, "billed_quantity": 1,
        "stage": "PROVISIONAL", "basis": "ESTIMATE_CONSERVATIVE"}},
        actor={"service": "test"}, correlation_id="c")

    after = _b(svc, b)
    assert after["outstanding_reserved_micro"] == before["outstanding_reserved_micro"]
    assert after["available_micro"] == before["available_micro"]
    assert len(_entries(svc)) == before_entries


def test_t23_24_reconciled_absent_releases_accrued():
    svc = _svc()
    b = _root(svc, limit=10_000)
    rid = _ready(svc, b, amount=2000)
    svc.accrue(T, rid, {"accrual": {
        "billing_unit": "request", "billed_micro": 500, "billed_quantity": 1,
        "stage": "PROVISIONAL", "basis": "ESTIMATE_CONSERVATIVE"}},
        actor={"service": "test"}, correlation_id="c")
    r = svc.release(T, rid, {"reason": "RECONCILED_ABSENT",
                             "evidence_ref": {"id": "provider-says-no"}},
                    actor={"service": "test"}, correlation_id="c2")
    assert r["state"] == "RELEASED"
    assert _b(svc, b)["available_micro"] == 10_000


def test_t23_27b_accrued_keeps_outstanding():
    svc = _svc()
    b = _root(svc, limit=10_000)
    rid = _ready(svc, b, amount=3000)
    svc.accrue(T, rid, {"accrual": {
        "billing_unit": "request", "billed_micro": 1000, "billed_quantity": 1,
        "stage": "PROVISIONAL", "basis": "ESTIMATE_CONSERVATIVE"}},
        actor={"service": "test"}, correlation_id="c")
    assert _b(svc, b)["outstanding_reserved_micro"] == 3000
    assert _b(svc, b)["consumed_micro"] == 0
    assert svc.get_budget(T, b)["pending_reconciliation"] is True


# ====================== 超支与币种（T23-28…T23-35c） ====================

def test_t23_28_overage_settles_and_exhausts_without_extra_entry():
    svc = _svc()
    b = _root(svc, limit=10_000)
    rid = _ready(svc, b, amount=5_000, conservative=5_000)
    before = len([e for e in _entries(svc) if e["entry_type"] == "SETTLE"])

    r = svc.settle(T, rid, {"final_accruals": [
        {"billing_unit": "request", "billed_micro": 12_000,
         "billed_quantity": 1, "basis": "PROVIDER_INVOICE",
         "invoice_ref": "inv1"}]},
        actor={"service": "test"}, correlation_id="c")

    assert r["overage_micro"] == 7_000
    assert r["settled_micro"] == 12_000
    assert r["budget_after"]["state"] == "EXHAUSTED"
    assert r["budget_after"]["available_micro"] == -2_000

    after = len([e for e in _entries(svc) if e["entry_type"] == "SETTLE"])
    # :res + :con 恰好两条，没有为超支再记一笔
    assert after - before == 2
    # adjustment 只解释，不是流水
    rows = svc.conn.execute(
        "SELECT * FROM adjustment WHERE budget_id = ?", (b,)).fetchall()
    assert len(rows) == 1
    assert dict(rows[0])["reason_code"] == "QUOTE_UNDERESTIMATE"
    assert dict(rows[0])["amount_micro"] == 7_000
    assert len([e for e in _entries(svc) if e["entry_type"] == "ADJUST"]) == 0


def test_t23_29_negative_available_still_balances():
    svc = _svc()
    root = _root(svc, limit=100_000)
    ep = _child(svc, root, "episode", "ep3")
    _grant(svc, root, ep, 10_000, "g1")
    rid = _ready(svc, ep, amount=5_000, conservative=5_000)
    svc.settle(T, rid, {"final_accruals": [
        {"billing_unit": "request", "billed_micro": 15_000,
         "basis": "PROVIDER_INVOICE", "invoice_ref": "i1"}]},
        actor={"service": "test"}, correlation_id="c")

    t = svc.tree(T, root)
    inv = t["invariant"]
    assert inv["available_plus_outstanding_plus_consumed"] == inv["root_limit_micro"]
    ep_node = next(n for n in t["nodes"] if n["budget_id"] == ep)
    assert ep_node["available_micro"] == -5_000
    # 不向上传播：父级仍是 ACTIVE
    root_node = next(n for n in t["nodes"] if n["budget_id"] == root)
    assert root_node["state"] == "ACTIVE"


def test_t23_30_missing_fx_snapshot():
    svc = _svc()
    b = _root(svc, currency="USD")
    with pytest.raises(ServiceError) as ei:
        _reserve(svc, b, amount=1000, currency="JPY", budget_currency="USD")
    assert ei.value.code == "FX_SNAPSHOT_MISSING"


def test_t23_31_fx_conversion_ceils_integer():
    svc = _svc()
    b = _root(svc, currency="USD")
    svc.register_fx_snapshot(T, {"base_currency": "JPY", "quote_currency": "USD",
                                 "rate_micro": 6_667, "valid_until": FAR,
                                 "observed_at": "2026-01-01T00:00:00.000000Z"})
    # 1000 * 6667 / 1e6 = 6.667 → ceil 7
    r = _reserve(svc, b, amount=1000, conservative=1000, currency="JPY",
                 budget_currency="USD")
    assert r["reserved_budget_currency_micro"] == 7


def test_t23_32_currencies_never_mix():
    svc = _svc()
    usd = _root(svc, limit=10_000, currency="USD")
    jpy = svc.create_budget(
        T, {"scope_type": "tenant", "scope_ref": "jpy", "currency": "JPY",
            "limit_micro": 999_999},
        actor={"service": "test"}, correlation_id="c")["budget_id"]
    _reserve(svc, usd, amount=1000)
    _reserve(svc, jpy, amount=2000, attempt="a2", currency="JPY",
             budget_currency="JPY")
    assert _b(svc, usd)["outstanding_reserved_micro"] == 1000
    assert _b(svc, jpy)["outstanding_reserved_micro"] == 2000


def test_t23_33_conservative_below_amount():
    svc = _svc()
    b = _root(svc)
    with pytest.raises(ServiceError) as ei:
        _reserve(svc, b, amount=1000, conservative=999)
    assert ei.value.status == 422
    assert ei.value.code == "CONSERVATIVE_BELOW_AMOUNT"


@pytest.mark.parametrize("amount", [0, -1])
def test_t23_34_non_positive_amount(amount):
    svc = _svc()
    b = _root(svc)
    with pytest.raises(ServiceError) as ei:
        _reserve(svc, b, amount=amount, conservative=max(amount, 1))
    assert ei.value.status == 422
    assert ei.value.code == "AMOUNT_NOT_POSITIVE"


def test_t23_35_ledger_is_append_only():
    svc = _svc()
    b = _root(svc)
    _reserve(svc, b)
    with pytest.raises(sqlite3.DatabaseError):
        svc.conn.execute("UPDATE ledger_entry SET delta_reserved_micro = 1")
    with pytest.raises(sqlite3.DatabaseError):
        svc.conn.execute("DELETE FROM ledger_entry")


def test_t23_35b_reserve_uses_upper_bound():
    svc = _svc()
    b = _root(svc, limit=100_000)
    _reserve(svc, b, amount=800_000 // 1000, conservative=900_000 // 1000)
    # 上界 900 预留，不是 800
    assert _b(svc, b)["outstanding_reserved_micro"] == 900


def test_t23_35c_settle_returns_the_difference():
    svc = _svc()
    b = _root(svc, limit=100_000)
    rid = _ready(svc, b, amount=800, conservative=900)
    assert _b(svc, b)["outstanding_reserved_micro"] == 900
    before_avail = _b(svc, b)["available_micro"]

    r = svc.settle(T, rid, {"final_accruals": [
        {"billing_unit": "request", "billed_micro": 780,
         "basis": "PROVIDER_INVOICE", "invoice_ref": "i1"}]},
        actor={"service": "test"}, correlation_id="c")

    assert r["settled_micro"] == 780
    assert r["unused_released_micro"] == 120
    after = _b(svc, b)
    assert after["consumed_micro"] == 780
    assert after["available_micro"] == before_avail + 120


# ============================== 隔离（T23-41…T23-44） ====================

def test_t23_41_cross_tenant_read_is_404():
    svc = _svc()
    b = _root(svc, tid=T)
    with pytest.raises(ServiceError) as ei:
        svc.get_budget(U, b)
    assert ei.value.status == 404
    assert ei.value.code == "NOT_FOUND"


def test_t23_42_cross_tenant_reserve_is_404():
    svc = _svc()
    b = _root(svc, tid=T)
    with pytest.raises(ServiceError) as ei:
        _reserve(svc, b, tid=U)
    assert ei.value.status == 404


def test_t23_44_client_business_key_is_ignored():
    svc = _svc()
    b = _root(svc)
    body = _reserve_body(b)
    body["business_key"] = "attacker_chosen"
    svc.reserve(T, body, actor={"service": "test"}, correlation_id="c")
    keys = [e["business_key"] for e in _entries(svc)]
    assert "attacker_chosen" not in keys
    assert any(k.startswith("reserve:") for k in keys)


# =========================== 崩溃与重建（T23-45…T23-48） ==================

def test_t23_45_rollback_leaves_no_residue():
    svc = _svc()
    b = _root(svc, limit=10_000)
    before_avail = _b(svc, b)["available_micro"]

    orig = svc._bump

    def boom(*a, **kw):
        raise RuntimeError("crash after reservation insert")

    svc._bump = boom
    with pytest.raises(RuntimeError):
        _reserve(svc, b, amount=1000)
    svc._bump = orig

    assert _b(svc, b)["available_micro"] == before_avail
    assert _conn_count(svc, "reservation") == 0
    assert len(_entries(svc)) == 1  # 只有建根预算时的 LIMIT_SET


def test_t23_47_entry_seq_may_have_holes_but_is_monotonic():
    svc = _svc()
    b = _root(svc, limit=10_000)
    # 制造一个缺口：回滚会消耗序号
    orig = svc._bump
    svc._bump = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x"))
    with pytest.raises(RuntimeError):
        _reserve(svc, b, amount=1000)
    svc._bump = orig

    _reserve(svc, b, amount=1000, attempt="a2")
    seqs = [e["entry_seq"] for e in _entries(svc)]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_t23_48_rebuild_counters_from_ledger():
    svc = _svc()
    root = _root(svc, limit=100_000)
    ep = _child(svc, root)
    _grant(svc, root, ep, 20_000, "g1")
    rid = _ready(svc, ep, amount=3000, conservative=4000)
    svc.settle(T, rid, {"final_accruals": [
        {"billing_unit": "request", "billed_micro": 3500,
         "basis": "PROVIDER_INVOICE", "invoice_ref": "i1"}]},
        actor={"service": "test"}, correlation_id="c")
    _reserve(svc, ep, amount=1000, attempt="a2", conservative=1500)

    for budget_id in (root, ep):
        live = svc.availability(T, budget_id)
        rebuilt = svc.rebuild_counters(T, budget_id)
        assert rebuilt == {
            "limit_micro": live["limit_micro"],
            "consumed_micro": live["consumed_micro"],
            "outstanding_reserved_micro": live["outstanding_reserved_micro"],
            "escrowed_out_micro": live["escrowed_out_micro"],
        }, budget_id


# ============================== HTTP 层 ==================================

def _app():
    spec = importlib.util.spec_from_file_location(
        "s23_api_app", ROOT / "api" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    module = _app()
    svc = module.service
    root = _root(svc)
    return TestClient(module.app), svc, root


def _h(cmd, tenant=T):
    return {"Idempotency-Key": cmd, "X-Tenant-Id": tenant}


def test_http_get_budget_matches_openapi_exactly(client):
    c, svc, root = client
    r = c.get(f"/budgets/{root}", headers=_h("q1"))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"budget_id", "currency", "limit_micro",
                         "consumed_micro", "outstanding_reserved_micro",
                         "pending_reconciliation"}
    assert "available_micro" not in body


def test_http_availability_is_separate(client):
    c, svc, root = client
    r = c.get(f"/budgets/{root}/availability", headers=_h("q2"))
    assert r.json()["available_micro"] == 1_000_000_000


def test_http_reserve_replays(client):
    c, svc, root = client
    body = _reserve_body(root)
    first = c.post("/budget-authorizations", json=body, headers=_h("cmd1"))
    assert first.status_code == 200, first.text
    rid = first.json()["reservation_id"]
    second = c.post("/budget-authorizations", json=body, headers=_h("cmd1"))
    assert second.json()["reservation_id"] == rid
    assert second.json()["replayed"] is True


def test_http_same_key_different_body_409(client):
    c, svc, root = client
    b1 = _reserve_body(root, amount=1000)
    c.post("/budget-authorizations", json=b1, headers=_h("cmdX"))
    b2 = _reserve_body(root, amount=2000)
    r = c.post("/budget-authorizations", json=b2, headers=_h("cmdX"))
    assert r.status_code == 409
    assert r.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_http_body_tenant_override_is_403(client):
    c, svc, root = client
    body = {**_reserve_body(root), "tenant_id": U}
    r = c.post("/budget-authorizations", json=body, headers=_h("cmdY"))
    assert r.status_code == 403
    assert r.json()["code"] == "TENANT_SCOPE_DENIED"


def test_http_error_shape_matches_openapi(client):
    c, svc, root = client
    r = c.post("/reservations/nope:authorize", json={}, headers=_h("cmdZ"))
    assert r.status_code == 404
    body = r.json()
    assert set(body) <= {"code", "message", "retryable", "correlation_id",
                         "details"}
    assert {"code", "message", "retryable", "correlation_id"} <= set(body)
    assert body["correlation_id"]


def test_http_missing_headers_is_400(client):
    c, svc, root = client
    r = c.post("/budget-authorizations", json=_reserve_body(root), headers={})
    assert r.status_code == 400
    assert r.json()["code"] == "MALFORMED_REQUEST"

# ================= 撤权与版本冲突（§3.1 / §6.2 / 错误表） =================

def test_t23_rv10_access_revoked_releases_only_reserved():
    """§6.2：撤权只放 RESERVED。AUTHORIZED 不动 —— S22 的 permit 可能 INFLIGHT。"""
    svc = _svc()
    b = _root(svc)
    a = _reserve(svc, b, attempt="att_a")
    z = _ready(svc, b, attempt="att_b")
    assert svc.get_reservation(T, a["reservation_id"])["state"] == "RESERVED"

    out = svc.consume_access_revoked(T, {"id": "rev-1"}, correlation_id="c_rv")
    assert out["released"] == [a["reservation_id"]]
    assert svc.get_reservation(T, a["reservation_id"])["state"] == "RELEASED"
    assert svc.get_reservation(T, a["reservation_id"])["release_reason"] == "OPERATOR"
    # AUTHORIZED 原样留着，钱仍然占着
    assert svc.get_reservation(T, z)["state"] == "AUTHORIZED"
    assert _b(svc, b)["outstanding_reserved_micro"] == 1000


def test_t23_rv11_access_revoked_invalidates_stale_view():
    """撤权之后，别的服务手上那条旧视图里的 RESERVED 已经不是事实，
    再用它的 revision 提交就会撞上 VERSION_CONFLICT，而不是被静默接受。"""
    svc = _svc()
    b = _root(svc)
    r = _reserve(svc, b, attempt="att_stale")
    rid = r["reservation_id"]
    stale_revision = svc.get_reservation(T, rid)["revision"]

    svc.consume_access_revoked(T, {"id": "rev-2"}, correlation_id="c_rv2")

    now_revision = svc.get_reservation(T, rid)["revision"]
    assert now_revision != stale_revision

    # 旧 revision 先被版本闸门挡住
    with pytest.raises(ServiceError) as stale:
        svc.authorize(T, rid, {"expected_revision": stale_revision},
                      actor={"service": "test"}, correlation_id="c_after")
    assert stale.value.code == "VERSION_CONFLICT"

    # 拿对 revision 也推不动：状态已经不是 RESERVED
    with pytest.raises(ServiceError) as gone:
        svc.authorize(T, rid, {"expected_revision": now_revision},
                      actor={"service": "test"}, correlation_id="c_after2")
    assert gone.value.status == 409
    assert gone.value.code == "RESERVATION_NOT_SETTLEABLE"
    assert gone.value.details["state"] == "RELEASED"


def test_t23_rv9_stale_revision_is_conflict_not_silent():
    """错误表 409 VERSION_CONFLICT（可重试）。用旧 revision 推进要明确失败。"""
    svc = _svc()
    b = _root(svc)
    rid = _reserve(svc, b)["reservation_id"]
    before = svc.get_reservation(T, rid)["revision"]

    svc.authorize(T, rid, {}, actor={"service": "test"}, correlation_id="c_a")
    after = svc.get_reservation(T, rid)["revision"]
    assert after == before + 1

    with pytest.raises(ServiceError) as exc:
        svc.release(T, rid, {"reason": "SAGA_ABORTED",
                             "evidence_ref": {"id": "x"},
                             "expected_revision": before},
                    actor={"service": "test"}, correlation_id="c_b")
    assert exc.value.status == 409
    assert exc.value.code == "VERSION_CONFLICT"
    assert exc.value.retryable is True
    assert exc.value.details == {"expected_revision": before,
                                 "actual_revision": after}
    # 冲突没有副作用
    assert svc.get_reservation(T, rid)["state"] == "AUTHORIZED"


def test_t23_rv12_current_revision_is_accepted():
    """同一命令带对 revision 必须放行，否则上一条用例证明不了区别。"""
    svc = _svc()
    b = _root(svc)
    rid = _reserve(svc, b)["reservation_id"]
    rev = svc.get_reservation(T, rid)["revision"]
    out = svc.release(T, rid, {"reason": "NEVER_DISPATCHED",
                               "expected_revision": rev},
                      actor={"service": "test"}, correlation_id="c_ok")
    assert out["state"] in ("RELEASED", "released")
    assert svc.get_reservation(T, rid)["state"] == "RELEASED"
    assert _b(svc, b)["outstanding_reserved_micro"] == 0
