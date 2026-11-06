"""Spread ledger, states and the single-leg gate : nothing here talks to Schwab."""
from datetime import date, datetime, timedelta, timezone

import pytest

from companion import trading
from companion.spreads.spreads import SpreadBook, leg_fills
from companion.store import Store
from companion.trading import Trader

from test_trading import Broker, _call_leg  # noqa: F401  (fakes shared with the trading tests)

SHORT_P, LONG_P = "SPXW  261105P07540000", "SPXW  261105P07535000"
LEGS = [{"symbol": SHORT_P, "side": "SHORT"}, {"symbol": LONG_P, "side": "LONG"}]
TODAY = date(2026, 11, 3)


@pytest.fixture
def book(tmp_path):
    return SpreadBook(Store(tmp_path / "s.db"))


def entry(oid=700, status="WORKING", fills=(), qty=2):
    """A put credit vertical entry; fills = [(short qty, long qty), ...] one activity each."""
    acts = [{"executionType": "FILL", "executionLegs": [{"legId": 1, "quantity": a, "price": 9.0},
                                                         {"legId": 2, "quantity": b, "price": 7.0}]}
            for a, b in fills]
    return {"orderId": oid, "status": status, "orderType": "NET_CREDIT", "quantity": qty,
            "orderLegCollection": [{"legId": 1, "instruction": "SELL_TO_OPEN", "instrument": {"symbol": SHORT_P}},
                                   {"legId": 2, "instruction": "BUY_TO_OPEN", "instrument": {"symbol": LONG_P}}],
            "orderActivityCollection": acts}


def close(oid=800, status="WORKING", fills=()):
    o = entry(oid, status, fills)
    o["orderLegCollection"][0]["instruction"], o["orderLegCollection"][1]["instruction"] = "BUY_TO_CLOSE", "SELL_TO_CLOSE"
    return o


def acct(short=0.0, long=0.0):
    return {SHORT_P: -short, LONG_P: long}


def state(book, sid):
    return book.get(sid)["state"]


def test_leg_fills_maps_execution_legs_to_contracts():
    assert leg_fills(entry(fills=[(1, 1), (1, 0)])) == {SHORT_P: 2.0, LONG_P: 1.0}


def test_a_contract_belongs_to_one_spread_only(book):
    a = book.create("t1", "$SPX", "PUT_CREDIT", LEGS, 2)
    assert a and book.owner_of([LONG_P])["spread_id"] == a
    other = [{"symbol": LONG_P, "side": "SHORT"}, {"symbol": "SPXW  261105P07530000", "side": "LONG"}]
    assert book.create("t2", "$SPX", "PUT_CREDIT", other, 1) is None
    assert book.owner_of(["SPXW  261105P07530000"]) is None          # nothing half-reserved


def test_lifecycle_pending_partial_open_closing_closed(book):
    sid = book.create("t1", "$SPX", "PUT_CREDIT", LEGS, 2)
    book.attach_order(sid, "entry", 700)
    book.reconcile([entry()], acct(), TODAY, True)
    assert state(book, sid) == "PENDING"
    book.reconcile([entry(fills=[(1, 1)])], acct(1, 1), TODAY, True)
    assert state(book, sid) == "PARTIAL"
    book.reconcile([entry(status="FILLED", fills=[(1, 1), (1, 1)])], acct(2, 2), TODAY, True)
    assert state(book, sid) == "OPEN"
    book.attach_order(sid, "close", 800)
    orders = [entry(status="FILLED", fills=[(1, 1), (1, 1)]), close(fills=[(1, 1)])]
    book.reconcile(orders, acct(1, 1), TODAY, True)
    assert state(book, sid) == "CLOSING"
    orders[1] = close(status="FILLED", fills=[(1, 1), (1, 1)])
    book.reconcile(orders, acct(), TODAY, True)
    assert state(book, sid) == "CLOSED" and book.owner_of([SHORT_P]) is None   # contracts released


def test_an_entry_cancelled_unfilled_releases_the_contracts(book):
    sid = book.create("t1", "$SPX", "PUT_CREDIT", LEGS, 2)
    book.attach_order(sid, "entry", 700)
    book.reconcile([entry(status="CANCELED")], acct(), TODAY, True)
    assert state(book, sid) == "CANCELED" and book.owner_of([LONG_P]) is None


def test_unbalanced_fills_after_the_entry_ends_need_action(book):
    sid = book.create("t1", "$SPX", "PUT_CREDIT", LEGS, 2)
    book.attach_order(sid, "entry", 700)
    book.reconcile([entry(status="CANCELED", fills=[(1, 0)])], acct(1, 0), TODAY, True)
    sp = book.get(sid)
    assert sp["state"] == "NEEDS_ACTION" and "UNBALANCED" in sp["detail"] and book.owner_of([SHORT_P])


def test_the_account_must_disagree_twice_before_broken(book):
    sid = book.create("t1", "$SPX", "PUT_CREDIT", LEGS, 2)
    book.attach_order(sid, "entry", 700)
    filled = [entry(status="FILLED", fills=[(2, 2)])]
    book.reconcile(filled, acct(2, 2), TODAY, True)
    book.reconcile(filled, acct(2, 0), TODAY, True)                 # long leg sold in thinkorswim?
    assert state(book, sid) == "OPEN" and "checking" in book.get(sid)["detail"]
    book.reconcile(filled, acct(2, 0), TODAY, True)
    sp = book.get(sid)
    assert sp["state"] == "BROKEN" and "account disagrees" in sp["detail"] and book.owner_of([SHORT_P])
    assert book.resolve(sid) and book.owner_of([SHORT_P]) is None


def test_stale_evidence_never_breaks_or_closes(book):
    sid = book.create("t1", "$SPX", "PUT_CREDIT", LEGS, 2)
    book.attach_order(sid, "entry", 700)
    filled = [entry(status="FILLED", fills=[(2, 2)])]
    book.reconcile(filled, acct(2, 2), TODAY, True)
    for _ in range(3):
        book.reconcile(filled, None, TODAY, False)
    assert state(book, sid) == "OPEN"


def test_fills_survive_a_restart_after_the_entry_leaves_the_order_read(book, tmp_path):
    sid = book.create("t1", "$SPX", "PUT_CREDIT", LEGS, 2)
    book.attach_order(sid, "entry", 700)
    book.reconcile([entry(status="FILLED", fills=[(2, 2)])], acct(2, 2), TODAY, True)
    again = SpreadBook(book.store)                                  # restart: new book, same database
    again.reconcile([], acct(2, 2), TODAY + timedelta(days=1), True)   # yesterday's entry is gone
    assert state(again, sid) == "OPEN"


def test_past_expiration_needs_a_settlement_check(book):
    sid = book.create("t1", "$SPX", "PUT_CREDIT", LEGS, 2)
    book.attach_order(sid, "entry", 700)
    book.reconcile([entry(status="FILLED", fills=[(2, 2)])], acct(2, 2), TODAY, True)
    book.reconcile([], acct(), date(2026, 11, 6), True)
    sp = book.get(sid)
    assert sp["state"] == "EXPIRED_CHECK" and book.owner_of([SHORT_P])


def test_a_final_spread_never_comes_back(book):
    sid = book.create("t1", "$SPX", "PUT_CREDIT", LEGS, 2)
    book.attach_order(sid, "entry", 700)
    book.reconcile([entry(status="CANCELED")], acct(), TODAY, True)
    assert not book.set_state(sid, "OPEN", "late echo")


# ---- the gate on single-leg actions -----------------------------------------------------------

@pytest.fixture
def trader(tmp_path, monkeypatch):
    t = Trader(Store(tmp_path / "t.db"))
    t._hash = "H" * 64
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    t.arm(True)
    sid = t.spreads.create("t1", "$SPX", "PUT_CREDIT", LEGS, 1)
    t.spreads.attach_order(sid, "entry", 700)
    t._client = Broker([entry(status="FILLED", fills=[(1, 1)])], position=(LONG_P, 1.0, "OPTION"),
                       ins={"putCall": "PUT", "underlyingSymbol": "$SPX"})
    return t


def test_single_leg_actions_refuse_a_spread_leg(trader):
    calls = [
        lambda: trader.close_position(LONG_P, [], set()),
        lambda: trader.close_position(LONG_P, [], set(), confirm_leg=True),      # confirm never bypasses
        lambda: trader.take_partial(LONG_P, 0.5, None, [], set(), confirm_leg=True),
        lambda: trader.move_stop(LONG_P, "1.00", [], set()),
        lambda: trader.set_target(LONG_P, "9.00", None, [], set(), None),
        lambda: trader.set_trail(LONG_P, "$", 1, "MARK", [], set(), None),
        lambda: trader.cancel_symbol_orders(LONG_P, [], set()),
        lambda: trader.cancel_entry(700),
    ]
    for call in calls:
        r = call()
        assert not r["ok"] and "leg of a companion spread" in r["error"], r
    assert trader._client.placed == [] and trader._client.cancelled == []


def test_auto_breakeven_skips_a_spread_leg(trader):
    trader.store.add_auto_be("tk", LONG_P)
    r = trader.auto_breakeven("tk", LONG_P, "LONG", 7.0, [], set())
    assert r["state"] == "SKIPPED" and trader._client.placed == []


def test_resolving_the_spread_frees_its_legs(trader):
    sid = trader.spreads.owner_of([LONG_P])["spread_id"]
    r = trader.resolve_spread(sid)                                  # PENDING: nothing to resolve yet
    assert not r["ok"] and "only for BROKEN" in r["error"]
    trader.spreads.set_state(sid, "BROKEN", "test")
    assert trader.resolve_spread(sid)["ok"]
    assert trader._spread_gate([LONG_P]) is None


def test_an_unclear_close_from_yesterday_still_blocks(trader):
    trader.store.log_action("close", "MXR", None, "UNKNOWN", "no answer")
    old = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with trader.store._lock, trader.store._con:
        trader.store._con.execute("update actions set created = ?", (old,))
    assert trader._unresolved_close("MXR") is not None


def test_the_poller_judges_spreads_and_shows_them(tmp_path, monkeypatch):
    from companion import poller as poller_mod
    from companion.poller import Poller

    class Day(datetime):          # the poller's "today" is the fixtures' day, before their contracts expire
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc).astimezone(tz) if tz else datetime(2026, 11, 3, 15, 0)
    monkeypatch.setattr(poller_mod, "datetime", Day)
    store = Store(tmp_path / "p.db")
    p = Poller(store)
    p.trader = Trader(store)
    monkeypatch.setattr(p, "_fresh", lambda name: True)
    monkeypatch.setattr(p, "_orders_fresh", lambda: True)
    sid = p.trader.spreads.create("t1", "$SPX", "PUT_CREDIT", LEGS, 1)
    p.trader.spreads.attach_order(sid, "entry", 700)
    p.orders_today = {700: entry(status="FILLED", fills=[(1, 1)], qty=1)}
    pos = [{"instrument": {"symbol": s, "assetType": "OPTION", "underlyingSymbol": "$SPX"},
            "longQuantity": q if q > 0 else 0, "shortQuantity": -q if q < 0 else 0, "averagePrice": 8.0,
            "marketValue": 0} for s, q in ((SHORT_P, -1.0), (LONG_P, 1.0))]
    p.account = {"securitiesAccount": {"positions": pos, "currentBalances": {}}}
    p._rebuild()
    (sp,) = p.state["spreads"]
    assert sp["spread_id"] == sid and sp["state"] == "OPEN"


# ---- audit pass 1 fixes -----------------------------------------------------------------------

def _filled_spread(book, oid=700):
    sid = book.create("t1", "$SPX", "PUT_CREDIT", LEGS, 2)
    book.attach_order(sid, "entry", oid)
    return sid


def test_an_entry_replaced_in_thinkorswim_keeps_the_contracts(book):
    sid = _filled_spread(book)
    book.reconcile([entry(status="REPLACED")], acct(), TODAY, True)
    assert state(book, sid) == "NEEDS_ACTION" and book.owner_of([LONG_P])


def test_filled_without_execution_details_is_not_cancelled(book):
    sid = _filled_spread(book)
    o = entry(status="FILLED")
    o["filledQuantity"] = 2
    book.reconcile([o], acct(), TODAY, True)                         # executions and account lag
    assert state(book, sid) == "UNKNOWN" and book.owner_of([LONG_P])


def test_an_unbalanced_close_remainder_needs_action(book):
    sid = _filled_spread(book)
    book.attach_order(sid, "close", 800)
    orders = [entry(status="FILLED", fills=[(2, 2)]), close(status="CANCELED", fills=[(1, 0)])]
    book.reconcile(orders, acct(1, 2), TODAY, True)                  # the account agrees: still not OPEN
    sp = book.get(sid)
    assert sp["state"] == "NEEDS_ACTION" and "UNBALANCED close" in sp["detail"]


def test_a_close_order_not_seen_yet_is_unknown(book):
    sid = _filled_spread(book)
    book.reconcile([entry(status="FILLED", fills=[(2, 2)])], acct(2, 2), TODAY, True)
    book.attach_order(sid, "close", 800)
    book.reconcile([entry(status="FILLED", fills=[(2, 2)])], acct(2, 2), TODAY, True)
    assert state(book, sid) == "UNKNOWN"


def test_an_older_order_is_followed_and_read_back_from_the_store(book):
    sid = _filled_spread(book)
    book.reconcile([entry(fills=[(1, 1)])], acct(1, 1), TODAY, True)          # seen working, part filled
    assert book.to_follow([]) == [700]                               # vanished while not final: ask Schwab
    book.store.save_orders([entry(status="FILLED", fills=[(1, 1), (1, 1)])])   # what the follow saved
    book.reconcile([], acct(2, 2), TODAY, True)
    assert state(book, sid) == "OPEN" and book.to_follow([]) == []


def test_the_same_account_read_counts_once_toward_broken(book):
    sid = _filled_spread(book)
    filled = [entry(status="FILLED", fills=[(2, 2)])]
    book.reconcile(filled, acct(2, 2), TODAY, True, observation="t0")
    for _ in range(3):
        book.reconcile(filled, acct(2, 0), TODAY, True, observation="t1")       # rebuilds, same account read
    assert state(book, sid) == "OPEN"
    book.reconcile(filled, acct(2, 0), TODAY, True, observation="t2")
    assert state(book, sid) == "BROKEN"


def test_resolve_refuses_while_an_own_order_is_live(book):
    sid = _filled_spread(book)
    book.reconcile([entry(fills=[(1, 1)])], acct(1, 1), TODAY, True)
    book.set_state(sid, "BROKEN", "test")
    ok, why = book.resolve(sid)
    assert not ok and "may still execute" in why and book.owner_of([LONG_P])


def test_the_app_runs_without_the_spreads_package(tmp_path):
    import subprocess
    import sys as _sys
    from pathlib import Path
    script = tmp_path / "public_check.py"
    script.write_text(
        "import sys, importlib.abc" + chr(10)
        + "class Block(importlib.abc.MetaPathFinder):" + chr(10)
        + "    def find_spec(self, name, path, target=None):" + chr(10)
        + "        if name.startswith('companion.spreads'): raise ImportError(name)" + chr(10)
        + "sys.meta_path.insert(0, Block())" + chr(10)
        + "from companion.store import Store" + chr(10)
        + "from companion.trading import Trader" + chr(10)
        + "from pathlib import Path" + chr(10)
        + "t = Trader(Store(Path(sys.argv[1])))" + chr(10)
        + "assert t.spreads is None and t._spread_gate(['X']) is None" + chr(10)
        + "print('ok')" + chr(10))
    import os
    repo = str(Path(__file__).resolve().parent.parent)
    out = subprocess.run([_sys.executable, str(script), str(tmp_path / "pub.db")], capture_output=True, text=True,
                         cwd=repo, env={**os.environ, "PYTHONPATH": repo})
    assert out.stdout.strip() == "ok", out.stderr


# ---- audit pass 2 fixes -----------------------------------------------------------------------

def test_resolve_refuses_while_any_working_order_touches_the_contracts(trader):
    sid = trader.spreads.owner_of([LONG_P])["spread_id"]
    trader.spreads.set_state(sid, "NEEDS_ACTION", "entry replaced")
    successor = {**entry(oid=701, status="WORKING"), "orderActivityCollection": []}   # new id, not linked
    trader._client.orders[701] = successor
    r = trader.resolve_spread(sid)
    assert not r["ok"] and "701" in r["error"] and trader.spreads.owner_of([LONG_P])
    successor["status"] = "CANCELED"
    assert trader.resolve_spread(sid)["ok"]


def test_a_second_close_never_seen_does_not_borrow_the_first_ones_status(book):
    sid = _filled_spread(book)
    book.attach_order(sid, "close", 800)
    book.reconcile([entry(status="FILLED", fills=[(2, 2)]), close(status="CANCELED", fills=[(1, 1)])],
                   acct(1, 1), TODAY, True)
    assert state(book, sid) == "OPEN"
    book.attach_order(sid, "close", 801)                            # the next close, response missing
    book.reconcile([entry(status="FILLED", fills=[(2, 2)])], acct(1, 1), TODAY, True)
    assert state(book, sid) == "UNKNOWN"


def test_a_final_order_with_missing_executions_is_asked_again(book):
    sid = _filled_spread(book)
    o = {**entry(status="FILLED", fills=[(1, 1)]), "filledQuantity": 2}
    book.store.save_orders([o])
    book.reconcile([], acct(2, 2), TODAY, True)
    assert state(book, sid) == "UNKNOWN" and book.to_follow([]) == [700]
    book.store.save_orders([{**entry(status="FILLED", fills=[(1, 1), (1, 1)]), "filledQuantity": 2}])
    book.reconcile([], acct(2, 2), TODAY, True)
    assert state(book, sid) == "OPEN" and book.to_follow([]) == []


def test_broken_waits_for_a_new_order_sweep_too(book):
    sid = _filled_spread(book)
    filled = [entry(status="FILLED", fills=[(2, 2)])]
    book.reconcile(filled, acct(2, 0), TODAY, True, observation=("a1", "s1"))
    book.reconcile(filled, acct(2, 0), TODAY, True, observation=("a2", "s1"))     # new account, same sweep
    assert state(book, sid) == "PENDING"
    book.reconcile(filled, acct(2, 0), TODAY, True, observation=("a3", "s2"))
    assert state(book, sid) == "BROKEN"


# ---- audit pass 3 fixes -----------------------------------------------------------------------

def test_resolve_reads_older_working_orders_fresh(trader):
    sid = trader.spreads.owner_of([LONG_P])["spread_id"]
    trader.spreads.set_state(sid, "NEEDS_ACTION", "entry replaced")
    old = {**entry(oid=650, status="WORKING"), "orderActivityCollection": []}   # a GTC from days ago
    real = trader._client.account_orders

    def orders(h, start, end, maxResults=None, status=None):
        resp = real(h, start, end, maxResults, status)
        if status == "WORKING" and (end - start).days > 30:                   # only the 59-day sweep sees it
            resp = type(resp)(200, [*resp.json(), old])
        return resp
    trader._client.account_orders = orders
    r = trader.resolve_spread(sid, orders=[])                                 # no cached snapshot at all
    assert not r["ok"] and "650" in r["error"]


def test_alternating_reads_still_reach_broken(book):
    sid = _filled_spread(book)
    filled = [entry(status="FILLED", fills=[(2, 2)])]
    for obs in (("a1", "s1"), ("a2", "s1"), ("a2", "s2")):
        book.reconcile(filled, acct(2, 0), TODAY, True, observation=obs)
    assert state(book, sid) == "BROKEN"


def test_a_later_close_with_missing_executions_is_unknown(book):
    sid = _filled_spread(book)
    book.attach_order(sid, "close", 800)
    book.reconcile([entry(status="FILLED", fills=[(2, 2)]), close(status="FILLED", fills=[(1, 1)])],
                   acct(1, 1), TODAY, True)
    book.attach_order(sid, "close", 801)
    second = {**close(oid=801, status="FILLED"), "filledQuantity": 1}           # executions not reported yet
    book.reconcile([entry(status="FILLED", fills=[(2, 2)]), second], acct(1, 1), TODAY, True)
    assert state(book, sid) == "UNKNOWN"
