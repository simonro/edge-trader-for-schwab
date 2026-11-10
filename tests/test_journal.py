"""Journal sync (companion/journal.py): settings, entries, payload, preview / apply.

The journal itself is faked: these tests check what Edge Trader sends and when.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from companion.journal import Journal, JournalError, is_local_url
from companion.store import Store

_ids = iter(range(5000, 99999))


def txn(utc, sym, qty, price, order_id, aid=None, fees=0.0):
    return {"activityId": aid or next(_ids), "time": utc, "type": "TRADE", "status": "VALID", "orderId": order_id,
            "transferItems": ([{"instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"}, "amount": fees,
                                "cost": -fees, "feeType": "COMMISSION"}] if fees else []) + [
                {"instrument": {"assetType": "EQUITY", "symbol": sym}, "amount": qty, "price": price,
                 "cost": -qty * price, "positionEffect": "OPENING" if qty > 0 else "CLOSING"}]}


def order(oid, sym, instruction, otype, entered, status="WORKING", price=None, stop=None, closed=None, children=()):
    o = {"orderId": oid, "enteredTime": entered, "status": status, "orderType": otype, "quantity": 100,
         "orderLegCollection": [{"instruction": instruction, "quantity": 100, "instrument": {"symbol": sym}}],
         "childOrderStrategies": list(children)}
    if price is not None:
        o["price"] = price
    if stop is not None:
        o["stopPrice"] = stop
    if closed:
        o["closeTime"] = closed
    return o


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S+0000")


class FakeJournal:
    def __init__(self):
        self.calls = []
        self.token = "tok1"
        self.conflict = False

    def __call__(self, method, path, body=None, url=None, timeout=None):
        self.calls.append((method, path, body))
        if path == "/api/sync/info":
            return 200, {"api": "edge-trader-sync", "api_version": 1}
        if path == "/api/accounts":
            return 200, [{"id": 1, "name": "Main"}, {"id": 4, "name": "Futures"}]
        if path == "/api/library":
            return 200, {"strategies": [{"name": "Breakout Long"}], "sources": [{"name": "Scanner"}],
                         "tags": {"mistake": [{"name": "moved stop"}]}}
        if path == "/api/sync/edge-trader":
            if body["dry_run"]:
                return 200, {"dry_run": True, "applied": False, "state_token": self.token, "fills": {"new": 2},
                             "events": {"new": 1}}
            if self.conflict:
                return 409, {"detail": "changed", "state_token": "tok2", "applied": False}
            assert body["state_token"] == self.token
            return 200, {"applied": True, "state_token": self.token, "fills": {"new": 2}, "backup": "b.db"}
        return 404, None


@pytest.fixture
def j(tmp_path):
    store = Store(tmp_path / "c.db")
    jr = Journal(store)
    jr._http = FakeJournal()
    jr.save_settings({"url": "http://127.0.0.1:8000", "routes": {"STOCK": 1, "OPTION": 1, "FUTURE": 4},
                      "dedup_accounts": [1, 2, 4], "exclude_symbols": "cohr, /es"})
    return jr


NOW = datetime.now(timezone.utc).replace(microsecond=0)
T0 = NOW - timedelta(hours=1)


def day_trade(store, entry_order=111):
    a, b = txn(iso(T0), "TSLA", 100, 380.0, entry_order, aid=7001), txn(iso(T0 + timedelta(minutes=5)), "TSLA", -100, 382.0, 222, aid=7002)
    store.save_transactions([a, b])
    return ["7001:0", "7002:0"]


def test_local_url_check():
    assert is_local_url("http://127.0.0.1:8000") and is_local_url("http://localhost:8000")
    assert not is_local_url("http://192.168.1.5:8000") and not is_local_url("https://journal.example.com")


def test_settings_validate_and_refuse_a_remote_journal_without_consent(j):
    s = j.settings()
    assert s["exclude_symbols"] == ["/ES", "COHR"] and s["routes"] == {"STOCK": 1, "OPTION": 1, "FUTURE": 4}
    with pytest.raises(JournalError, match="not on this computer"):
        j.save_settings({"url": "http://192.168.1.5:8000"})
    assert j.save_settings({"url": "http://192.168.1.5:8000", "allow_remote": True})["allow_remote"] is True
    with pytest.raises(JournalError):
        j.save_settings({"url": "ftp://127.0.0.1"})


def test_test_reads_version_and_accounts(j):
    out = j.test()
    assert [a["name"] for a in out["accounts"]] == ["Main", "Futures"]


def test_entries_notes_and_day_note(j):
    ids = day_trade(j.store)
    e = j.save_entry("TSLA", ids, {"strategy": "Breakout Long", "emotional_state": "calm"}, {"mistake": ["moved stop"]})
    assert e["id"] == "t7001" and e["fields"]["strategy"] == "Breakout Long"
    e = j.save_entry("TSLA", ids, {"strategy": ""}, None)
    assert "strategy" not in e["fields"] and e["tags"] == {"mistake": ["moved stop"]}
    e = j.add_note("TSLA", ids, "held VWAP")
    assert [n["text"] for n in e["notes"]] == ["held VWAP"]
    j.edit_note(e["notes"][0]["id"], None)
    assert j.get_entry(ids)["notes"] == []
    assert j.save_day_note("2026-11-04", "plan: A setups only")["text"] == "plan: A setups only"
    with pytest.raises(JournalError):
        j.save_entry("TSLA", ids, {"pnl": 1}, None)
    with pytest.raises(JournalError):
        j.annotation_id([])


def test_payload_carries_fills_facts_events_notes(j):
    ids = day_trade(j.store)
    j.store.insert_ticket({"ticket_id": "tk1", "symbol": "TSLA", "side": "LONG", "qty": 100, "limit": "380",
                           "stop": "378.5", "target": "386", "risk": 150.0, "request": {}})
    j.store.update_ticket("tk1", "ACCEPTED", order_id=111)
    e1 = iso(T0 + timedelta(seconds=2))
    e2 = iso(T0 + timedelta(minutes=2))
    j.store.save_orders([
        order(301, "TSLA", "SELL", "STOP", e1, status="REPLACED", stop=378.5, closed=e2),
        order(302, "TSLA", "SELL", "STOP", e2, status="CANCELED", stop=380.0, closed=iso(T0 + timedelta(minutes=5))),
        order(303, "TSLA", "SELL", "LIMIT", e1, status="CANCELED", price=386.0, closed=iso(T0 + timedelta(minutes=5))),
        order(304, "TSLA", "BUY", "LIMIT", iso(T0), status="FILLED", price=380.0),       # the entry: not an exit
    ])
    j.save_entry("TSLA", ids, {"strategy": "Breakout Long"}, None)
    j.add_note("TSLA", ids, "moved to B/E")
    j.save_day_note(datetime.now().date().isoformat(), "good day")
    payload, meta = j.build_payload()
    assert [t["activityId"] for t in payload["transactions"]] == [7001, 7002]
    assert payload["dedup_accounts"] == [1, 2, 4] and payload["exclude_symbols"] == ["/ES", "COHR"]
    (ann,) = payload["annotations"]
    assert ann["id"] == "t7001" and ann["fill_keys"] == ["7001", "7002"]
    f = ann["fields"]
    assert (f["stop_loss"], f["target_price"], f["risk_per_trade"], f["strategy"]) == (378.5, 386.0, 150.0, "Breakout Long")
    assert f["risk_reward"] == 4.0 and f["r_multiple"] == round(200.0 / 150.0, 2)
    kinds = [(e["id"], e["kind"]) for e in payload["events"]]
    assert ("o301:set", "stop_set") in kinds and ("o302:set", "breakeven") in kinds
    assert ("o303:set", "target_set") in kinds and ("o303:cancel", "target_cancelled") in kinds
    assert ("o302:cancel", "stop_cancelled") in kinds and not any(i.startswith("o304") for i, _ in kinds)
    assert any(e["kind"] == "note" and e["text"] == "moved to B/E" for e in payload["events"])
    assert payload["day_notes"][0]["account_id"] == 1 and meta["trades"] == 1


def test_tos_trade_takes_its_stop_from_the_first_stop_order(j):
    day_trade(j.store)
    j.store.save_orders([order(401, "TSLA", "SELL", "STOP", iso(T0 + timedelta(seconds=30)), stop=379.0,
                               status="CANCELED", closed=iso(T0 + timedelta(minutes=5)))])
    payload, _ = j.build_payload()
    f = payload["annotations"][0]["fields"]
    assert f["stop_loss"] == 379.0 and f["risk_per_trade"] == 100.0 and f["r_multiple"] == 2.0


def test_preview_then_apply_sends_the_same_payload_and_moves_the_cursor(j):
    day_trade(j.store)
    pre = j.preview()
    assert pre["state_token"] == "tok1" and j.settings()["last_sync_day"] is None
    out = j.apply("tok1")
    from zoneinfo import ZoneInfo
    assert out["applied"] and j.settings()["last_sync_day"] == datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    sent = [c[2] for c in j._http.calls if c[1] == "/api/sync/edge-trader"]
    assert sent[0]["transactions"] == sent[1]["transactions"] and sent[1]["state_token"] == "tok1"
    assert j.settings()["last_sync_at"]
    with pytest.raises(JournalError, match="out of date"):
        j.apply("tok1")


def test_conflict_keeps_the_cursor_and_offers_a_fresh_token(j):
    day_trade(j.store)
    j.preview()
    j._http.conflict = True
    out = j.apply("tok1")
    assert out["conflict"] and out["state_token"] == "tok2" and j.settings()["last_sync_day"] is None


def test_not_configured_refuses(tmp_path):
    jr = Journal(Store(tmp_path / "c.db"))
    with pytest.raises(JournalError, match="journal address"):
        jr.build_payload()


def test_demo_mode_never_syncs(tmp_path):
    jr = Journal(Store(tmp_path / "c.db"), demo=True)
    with pytest.raises(JournalError, match="Demo"):
        jr.preview()


# ---- regressions from the independent audit (2026-11-06) ----

def test_an_edit_made_after_the_preview_goes_out_next_time(j):
    ids = day_trade(j.store)
    j.save_entry("TSLA", ids, {"strategy": "Breakout Long"}, None)
    j.preview()
    import time as _t
    _t.sleep(1.1)
    j.save_entry("TSLA", ids, {"emotional_state": "calm"}, None)      # after the payload was built
    j.apply("tok1")
    s = j.settings()
    s["last_sync_day"] = "2000-01-01"          # push the trade out of the window
    j.store.set_setting("journal", json.dumps(s))
    payload, _ = j.build_payload()
    (ann,) = [a for a in payload["annotations"] if a["id"] == "t7001"]
    assert ann["fields"]["emotional_state"] == "calm"


def test_deleted_note_is_sent_until_an_apply_succeeds(j):
    ids = day_trade(j.store)
    note = j.add_note("TSLA", ids, "first idea")["notes"][0]
    j.edit_note(note["id"], None)
    payload, _ = j.build_payload()
    assert payload["deleted_events"] == [note["id"]]
    j.preview()
    j.apply("tok1")
    assert j.build_payload()[0]["deleted_events"] == []


def test_entry_oco_children_count_even_when_the_entry_filled_late(j):
    day_trade(j.store, entry_order=111)
    placed = iso(T0 - timedelta(seconds=90))             # entry rested 90 s before it filled
    child_stop = order(112, "TSLA", "SELL", "STOP", placed, status="CANCELED", stop=378.0,
                       closed=iso(T0 + timedelta(minutes=5)))
    entry = order(111, "TSLA", "BUY", "LIMIT", placed, status="FILLED", price=380.0, children=[child_stop])
    j.store.save_orders([entry])
    payload, _ = j.build_payload()
    (ann,) = payload["annotations"]
    assert ann["fields"]["stop_loss"] == 378.0
    assert ("o112:set", "stop_set") in [(e["id"], e["kind"]) for e in payload["events"]]


def test_an_entry_is_found_by_any_of_its_fills(j):
    ids = day_trade(j.store)
    j.save_entry("TSLA", ["7002:0"], {"strategy": "Breakout Long"}, None)   # saved from a view that saw 7002 first
    assert j.get_entry(ids)["fields"]["strategy"] == "Breakout Long"
    payload, _ = j.build_payload()
    assert [a["id"] for a in payload["annotations"]] == ["t7002"]


def test_a_non_json_answer_is_explained(tmp_path, monkeypatch):
    import io
    import urllib.request
    jr = Journal(Store(tmp_path / "c.db"))
    jr.save_settings({"url": "http://127.0.0.1:8000", "routes": {"STOCK": 1}})

    class Resp(io.BytesIO):
        status = 200
        def __enter__(self): return self
        def __exit__(self, *a): return False
    import types
    from companion.journal import sync as jsync
    monkeypatch.setattr(jsync, "_OPENER", types.SimpleNamespace(open=lambda req, timeout=None: Resp(b"<html>hi</html>")))
    with pytest.raises(JournalError, match="not like the journal"):
        jr.test()


def test_a_timeout_says_the_apply_may_have_gone_through(tmp_path, monkeypatch):
    import urllib.request
    jr = Journal(Store(tmp_path / "c.db"))
    jr.save_settings({"url": "http://127.0.0.1:8000", "routes": {"STOCK": 1}})

    def slow(req, timeout=None):
        raise TimeoutError("timed out")
    import types
    from companion.journal import sync as jsync
    monkeypatch.setattr(jsync, "_OPENER", types.SimpleNamespace(open=slow))
    with pytest.raises(JournalError, match="still have gone through"):
        jr.test()


def test_oco_rebuild_noise_is_collapsed():
    from companion.journal import _collapse
    ev = [{"id": "a", "ts": "2026-11-04T15:00:00+00:00", "kind": "target_set", "price": 271.42},
          {"id": "b", "ts": "2026-11-04T15:00:00+00:00", "kind": "stop_set", "price": 264.73},
          {"id": "c", "ts": "2026-11-04T15:10:00+00:00", "kind": "target_cancelled", "price": 271.42},
          {"id": "d", "ts": "2026-11-04T15:10:01+00:00", "kind": "target_moved", "price": 271.42},
          {"id": "e", "ts": "2026-11-04T15:10:01+00:00", "kind": "stop_cancelled", "price": 264.73},
          {"id": "f", "ts": "2026-11-04T15:10:02+00:00", "kind": "breakeven", "price": 266.96},
          {"id": "g", "ts": "2026-11-04T15:20:00+00:00", "kind": "target_cancelled", "price": 271.42}]
    assert [(e["id"], e["kind"]) for e in _collapse(ev)] == [
        ("a", "target_set"), ("b", "stop_set"), ("f", "breakeven"), ("g", "target_cancelled")]


def test_evening_futures_fills_count_for_their_trading_day(j):
    """A futures fill at 21:47 ET on Nov 5 has Schwab tradeDate Nov 6: a sync starting Nov 6 sends it."""
    def fut(utc, qty, price, aid):
        return {"activityId": aid, "time": utc, "type": "TRADE", "status": "VALID", "orderId": aid + 1,
                "tradeDate": "2026-11-06T05:00:00+0000",
                "transferItems": [{"instrument": {"assetType": "FUTURE", "symbol": "/MNQZ26"}, "amount": qty,
                                   "price": price, "cost": 0.0}]}
    j.store.save_transactions([fut("2026-11-06T02:43:39+0000", 5, 27094.4, 8101),
                               fut("2026-11-06T02:47:48+0000", -5, 27109.3, 8103)])
    s = j.settings()
    s["first_sync_start"] = "2026-11-06"
    j.store.set_setting("journal", json.dumps(s))
    payload, meta = j.build_payload()
    assert {x["activityId"] for x in payload["transactions"]} == {8101, 8103}
    assert meta["trades"] == 1


def test_a_picked_account_covers_the_whole_round_trip(j):
    """Picking an account for a trade in the preview sends every fill of that round trip there,
    including a fill that joins the trade later."""
    def fut(utc, qty, price, aid):
        return {"activityId": aid, "time": utc, "type": "TRADE", "status": "VALID", "orderId": aid + 1,
                "tradeDate": "2026-11-06T05:00:00+0000",
                "transferItems": [{"instrument": {"assetType": "FUTURE", "symbol": "/MNQZ26"}, "amount": qty,
                                   "price": price, "cost": 0.0}]}
    j.store.save_transactions([fut("2026-11-06T02:43:39+0000", 5, 27094.4, 8101)])
    s = j.settings()
    s["first_sync_start"] = "2026-11-06"
    j.store.set_setting("journal", json.dumps(s))
    j.set_account(["8101"], 4)
    j.store.save_transactions([fut("2026-11-06T02:47:48+0000", -5, 27109.3, 8103)])
    payload, _ = j.build_payload()
    assert payload["account_overrides"] == {"8101": 4, "8103": 4}
    j.set_account(["8103"], None)          # clearing from the later fill clears the whole round trip
    payload, _ = j.build_payload()
    assert payload["account_overrides"] == {} and j.overrides() == {}
    j.set_account(["8103:0"], 2)            # a multi-leg style key counts by its activityId
    assert j.overrides() == {"8101": 2, "8103": 2}


# ---- companion spreads: one journal entry per spread ----------------------------------------------

def opt_txn(utc, sym, qty, price, order_id, aid):
    return {"activityId": aid, "time": utc, "type": "TRADE", "status": "VALID", "orderId": order_id,
            "transferItems": [{"instrument": {"assetType": "OPTION", "symbol": sym, "underlyingSymbol": "MU"},
                               "amount": qty, "price": price, "cost": -qty * price * 100,
                               "positionEffect": "OPENING" if qty < 0 or sym.endswith("5000") else "CLOSING"}]}


SHORT, LONG = "MU    261109P00737500", "MU    261109P00735000"


def mu_spread(store, closed=True):
    """The 2026-11-09 MU put credit: sold 737.5 at 6.98, bought 735 at 5.56 (1.42 credit, width 2.50),
    closed by the app stop at 9.12 / 7.22 (1.90 debit)."""
    from companion.spreads.spreads import SpreadBook
    book = SpreadBook(store)
    t1, t2 = T0 + timedelta(seconds=10), T0 + timedelta(minutes=14)
    txns = [opt_txn(iso(t1), SHORT, -2, 6.98, 900, 8001), opt_txn(iso(t1), LONG, 2, 5.56, 900, 8002)]
    if closed:
        txns += [opt_txn(iso(t2), SHORT, 2, 9.12, 901, 8003), opt_txn(iso(t2), LONG, -2, 7.22, 901, 8004)]
    store.save_transactions(txns)
    with store._lock, store._con:
        store._con.execute("insert into spreads (spread_id, kind, qty, state, entry_order_id, close_order_id, created) "
                           "values ('sp-mu', 'PUT_CREDIT', 2, ?, 900, ?, ?)",
                           ("CLOSED" if closed else "OPEN", 901 if closed else None, iso(T0)))
        store._con.execute("insert into spread_tickets (spread_id, ticket_id, put_call, width) values ('sp-mu', 'tk', 'PUT', 2.5)")
        for oid, role in ((900, "entry"), (901, "close")):
            for sym in (SHORT, LONG):
                store._con.execute("insert into spread_fills values ('sp-mu', ?, ?, ?, 2, 'FILLED', ?)", (oid, role, sym, iso(T0)))
    return book, txns


def test_a_spread_gets_its_own_fill_ids_first_entry_fill_first(tmp_path):
    from companion.spreads.spreads import SpreadBook
    store = Store(tmp_path / "c.db")
    store.save_transactions([
        opt_txn(iso(T0 + timedelta(minutes=14)), SHORT, 2, 9.12, 901, 8003),
        opt_txn(iso(T0 - timedelta(days=90)), LONG, 2, 5.56, 900, 8002),      # held for months: still found
        opt_txn(iso(T0 - timedelta(days=90)), SHORT, -2, 6.98, 900, 8001),
        opt_txn(iso(T0), "MU    261109P00730000", 1, 1.0, 555, 8999)])        # another order: not the spread's
    (v,) = SpreadBook(store).attach_fill_ids([{"spread_id": "sp-mu", "order_ids": [900, 901]}])
    assert v["fill_ids"] == ["8001:0", "8002:0", "8003:0"]


def test_the_view_lists_every_order_the_spread_sent(tmp_path):
    book, _ = mu_spread(Store(tmp_path / "c.db"))
    assert book.view(book.get("sp-mu"))["order_ids"] == [900, 901]


def test_a_spread_syncs_as_one_annotation_with_its_risk_and_r(j):
    mu_spread(j.store)
    ids = ["8001:0", "8002:0", "8003:0", "8004:0"]
    j.save_entry("MU", ids, {"strategy": "Put credit"}, None)
    j.add_note("MU", ids, "stop at 1.75, sent when hit")
    payload, meta = j.build_payload()
    (ann,) = payload["annotations"]                       # one entry, not one per leg
    assert ann["id"] == "t8001" and ann["fill_keys"] == ["8001", "8002", "8003", "8004"]
    f = ann["fields"]
    assert f["strategy"] == "Put credit"
    assert f["risk_per_trade"] == 216.0                   # (2.50 - 1.42) x 2 x 100
    assert f["r_multiple"] == round(-96.0 / 216.0, 2)     # (1.42 - 1.90) x 2 x 100
    notes = [e for e in payload["events"] if e["kind"] == "note"]
    assert [n["text"] for n in notes] == ["stop at 1.75, sent when hit"]   # once, not once per leg
    assert not [e for e in payload["events"] if e["kind"] != "note"] and meta["trades"] == 2


def test_an_open_spread_has_risk_but_no_r_yet(j):
    mu_spread(j.store, closed=False)
    j.add_note("MU", ["8001:0", "8002:0"], "in at 1.42")
    (ann,) = j.build_payload()[0]["annotations"]
    assert ann["fields"] == {"risk_per_trade": 216.0} and ann["fill_keys"] == ["8001", "8002"]


def test_without_a_spreads_ledger_legs_stay_separate_trades(j):
    j.store.save_transactions([opt_txn(iso(T0), SHORT, -1, 6.98, 900, 8101),
                               opt_txn(iso(T0 + timedelta(minutes=1)), SHORT, 1, 6.00, 902, 8102)])
    j.add_note("MU", ["8101:0"], "single leg")
    (ann,) = j.build_payload()[0]["annotations"]
    assert ann["id"] == "t8101" and "risk_per_trade" not in ann["fields"]


def test_a_thinkorswim_add_on_a_spread_contract_stays_out_of_the_spread(j):
    mu_spread(j.store)
    j.store.save_transactions([opt_txn(iso(T0 + timedelta(minutes=5)), SHORT, -1, 7.50, 777, 8050),   # tos add
                               opt_txn(iso(T0 + timedelta(minutes=13)), SHORT, 1, 9.00, 778, 8051)])
    j.add_note("MU", ["8001:0", "8002:0"], "spread note")
    (ann,) = j.build_payload()[0]["annotations"]
    assert ann["fill_keys"] == ["8001", "8002", "8003", "8004"]     # not 8050 / 8051
    assert "risk_per_trade" not in ann["fields"]                    # its leg is not clean: no spread numbers


def test_legs_the_journal_would_not_pair_get_no_spread_numbers(j):
    mu_spread(j.store, closed=False)
    with j.store._lock, j.store._con:              # the long leg filled a second later: two separate journal legs
        j.store._con.execute("delete from transactions where activity_id = 8002")
    j.store.save_transactions([opt_txn(iso(T0 + timedelta(seconds=11)), LONG, 2, 5.56, 900, 8002)])
    j.add_note("MU", ["8001:0", "8002:0"], "in")
    (ann,) = j.build_payload()[0]["annotations"]
    assert ann["fields"] == {} and ann["fill_keys"] == ["8001", "8002"]


def test_unbalanced_legs_get_no_spread_numbers(j):
    mu_spread(j.store, closed=False)
    with j.store._lock, j.store._con:
        j.store._con.execute("delete from transactions where activity_id = 8002")
    j.store.save_transactions([opt_txn(iso(T0 + timedelta(seconds=10)), LONG, 1, 5.56, 900, 8002)])
    j.add_note("MU", ["8001:0", "8002:0"], "in")
    (ann,) = j.build_payload()[0]["annotations"]
    assert ann["fields"] == {}


def test_a_spread_gets_a_timeline_from_its_orders_and_app_stop(j):
    mu_spread(j.store)
    t = lambda m: iso(T0 + timedelta(minutes=m))
    spread_order = lambda oid, otype, price, entered, status, closed=None, filled=0: {
        "orderId": oid, "orderType": otype, "complexOrderStrategyType": "VERTICAL", "price": price, "status": status,
        "enteredTime": entered, "filledQuantity": filled, **({"closeTime": closed} if closed else {}),
        "orderLegCollection": [{"instruction": "BUY_TO_CLOSE", "quantity": 2, "instrument": {"symbol": SHORT}},
                               {"instruction": "SELL_TO_CLOSE", "quantity": 2, "instrument": {"symbol": LONG}}]}
    j.store.save_orders([
        spread_order(950, "NET_DEBIT", 0.71, t(2), "CANCELED", closed=t(5)),              # target, moved at 5
        spread_order(951, "NET_DEBIT", 0.60, t(5), "CANCELED", closed=t(13)),             # cancelled for the close
        spread_order(952, "NET_DEBIT", 0.50, t(6), "CANCELED", closed=t(7)),              # a close moved away
        spread_order(901, "NET_DEBIT", 1.92, t(14), "FILLED", closed=t(14), filled=2)])
    with j.store._lock, j.store._con:
        for oid, role in ((950, "target"), (951, "target"), (952, "close")):
            for sym in (SHORT, LONG):
                j.store._con.execute("insert into spread_fills values ('sp-mu', ?, ?, ?, 0, 'CANCELED', ?)", (oid, role, sym, t(0)))
        j.store._con.execute("create table if not exists spread_stop_events (spread_id text, created text, kind text, mid real, detail text)")
        for created, kind, mid, detail in ((t(3), "set", None, "stop 1.75 (0.3R), send when hit"),
                                           (t(13.9), "would_fire", 1.75, "stop 1.75"),
                                           (t(14), "sent", None, "close order 901 for 2 at 1.92")):
            j.store._con.execute("insert into spread_stop_events values ('sp-mu', ?, ?, ?, ?)", (created, kind, mid, detail))
    evs = [(e["kind"], e["price"]) for e in j._spread_events({"spread_id": "sp-mu"})]
    assert evs == [("target_set", 0.71), ("stop_set", 1.75), ("target_moved", 0.60), ("other", 0.5),
                   ("target_cancelled", 0.60), ("other", 1.75), ("close", 1.92), ("other", None)]
    payload, _ = j.build_payload()
    (ann,) = payload["annotations"]                       # a timeline alone is enough to send the spread
    assert ann["id"] == "t8001" and sum(1 for e in payload["events"] if e["annotation_id"] == "t8001") == len(evs)


def test_net_fill_price_comes_from_the_execution_legs():
    from companion.journal.sync import _net_fill
    o = {"orderLegCollection": [{"legId": 1, "instruction": "BUY_TO_CLOSE", "quantity": 2},
                                {"legId": 2, "instruction": "SELL_TO_CLOSE", "quantity": 2}],
         "orderActivityCollection": [{"executionLegs": [{"legId": 1, "price": 9.12, "quantity": 2},
                                                        {"legId": 2, "price": 7.22, "quantity": 2}]}]}
    assert _net_fill(o) == 1.9
    o["orderActivityCollection"][0]["executionLegs"].pop()          # one leg not listed yet: no net price
    assert _net_fill(o) is None


def test_spread_timeline_keeps_mode_changes_and_shows_target_and_partial_fills(j):
    mu_spread(j.store)
    t = lambda m: iso(T0 + timedelta(minutes=m))
    legs = [{"legId": 1, "instruction": "BUY_TO_CLOSE", "quantity": 2, "instrument": {"symbol": SHORT}},
            {"legId": 2, "instruction": "SELL_TO_CLOSE", "quantity": 2, "instrument": {"symbol": LONG}}]
    j.store.save_orders([
        {"orderId": 960, "orderType": "NET_DEBIT", "price": 0.71, "status": "FILLED", "enteredTime": t(2),
         "closeTime": t(9), "filledQuantity": 2, "orderLegCollection": legs,
         "orderActivityCollection": [{"executionLegs": [{"legId": 1, "price": 3.0, "quantity": 2},
                                                        {"legId": 2, "price": 2.35, "quantity": 2}]}]},
        {"orderId": 961, "orderType": "NET_DEBIT", "price": 1.10, "status": "CANCELED", "enteredTime": t(4),
         "closeTime": t(5), "filledQuantity": 1, "orderLegCollection": legs}])
    with j.store._lock, j.store._con:
        for oid, role in ((960, "target"), (961, "close")):
            j.store._con.execute("insert into spread_fills values ('sp-mu', ?, ?, ?, 0, 'X', ?)", (oid, role, SHORT, t(0)))
        j.store._con.execute("create table if not exists spread_stop_events (spread_id text, created text, kind text, mid real, detail text)")
        for created, detail in ((t(1), "stop 1.75 (0.3R)"), (t(3), "stop 1.75 (0.3R), send when hit")):
            j.store._con.execute("insert into spread_stop_events values ('sp-mu', ?, 'set', NULL, ?)", (created, detail))
    evs = [(e["kind"], e["price"], e["text"]) for e in j._spread_events({"spread_id": "sp-mu"})]
    assert ("stop_set", 1.75, "app stop (no order at Schwab), alert only") in evs
    assert ("stop_moved", 1.75, "app stop (no order at Schwab), sends a close when hit") in evs   # kept: mode changed
    assert ("close", 0.65, "spread target filled") in evs                                     # 3.00 - 2.35
    assert ("close", 1.10, "spread close filled, 1 of 2, rest canceled (limit price; fill price not listed)") in evs


# ---- public-release hardening (ChatGPT audit 2026-11-10) -------------------------------------------

def test_a_dot_localhost_name_is_not_local():
    assert not is_local_url("http://journal.localhost:8010")


def test_test_refuses_a_remote_address_without_consent(j):
    with pytest.raises(JournalError, match="not on this computer"):
        j.test("http://192.168.1.5:8010")
    assert not j._http.calls                                   # nothing was contacted
    j.test("http://192.168.1.5:8010", allow_remote=True)
    assert j._http.calls


def test_accounts_from_the_journal_are_numbers_and_text_only(j):
    j._http.__class__ = type("Odd", (FakeJournal,), {"__call__": lambda self, m, p, body=None, url=None, timeout=None:
        (200, {"api": "edge-trader-sync", "api_version": 1, "x": "<img>"}) if p == "/api/sync/info" else
        (200, [{"id": "<img src=x onerror=1>", "name": "bad"}, {"id": "7", "name": 5}])})
    r = j.test()
    assert r["accounts"] == [{"id": 7, "name": "5"}] and "x" not in r["info"]


def test_excluded_symbols_and_account_numbers_never_leave(j):
    a = txn(iso(T0), "COHR", 10, 100.0, 9001)
    b = {**txn(iso(T0 + timedelta(minutes=1)), "AAPL", 10, 200.0, 9002), "accountNumber": "12345678"}
    j.store.save_transactions([a, b])
    payload, meta = j.build_payload()
    syms = [t["transferItems"][-1]["instrument"]["symbol"] for t in payload["transactions"]]
    assert "COHR" not in syms and "AAPL" in syms
    assert all("accountNumber" not in t for t in payload["transactions"])
    assert meta["excluded_here"] == 1
    p = j.preview()
    assert p["fills"]["excluded"] == 1                          # counted, though never sent


def test_redirects_are_refused(tmp_path, monkeypatch):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://example.com/api/sync/info")
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        jr = Journal(Store(tmp_path / "c.db"))
        jr.save_settings({"url": f"http://127.0.0.1:{srv.server_port}", "routes": {"STOCK": 1}})
        with pytest.raises(JournalError, match="HTTP 302"):
            jr.test()
    finally:
        srv.shutdown()


def test_an_excluded_trade_sends_no_entry_events_or_notes(j):
    a = txn(iso(T0), "COHR", 10, 100.0, 9101, aid=88001)
    b = txn(iso(T0 + timedelta(minutes=5)), "COHR", -10, 101.0, 9102, aid=88002)
    j.store.save_transactions([a, b])
    j.save_entry("COHR", ["88001:0", "88002:0"], {"strategy": "Breakout Long"}, None)
    j.add_note("COHR", ["88001:0", "88002:0"], "private thought")
    payload, _ = j.build_payload()
    assert payload["transactions"] == []
    assert payload["annotations"] == [] and payload["events"] == []
