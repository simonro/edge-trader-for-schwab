"""Spread ticket, cancel and close (slice 2) with a fake Schwab: nothing here talks to Schwab."""
from datetime import date
from decimal import Decimal

import pytest

from companion import trading
from companion.spreads import spread_desk
from companion.spreads.spread_desk import (SpreadDesk, close_order, entry_order, make_plan, on_step, same_underlying,
                                           spread_step)
from companion.store import Store
from companion.trading import Trader

from test_trading import JsonResp, Resp

HI_P, LO_P = "NFLX  261106P00610000", "NFLX  261106P00605000"        # put credit: sell 610, buy 605
LO_C, HI_C = "NFLX  261106C00650000", "NFLX  261106C00655000"        # call debit: buy 650, sell 655
SPX_HI, SPX_LO = "SPXW  261106P06800000", "SPXW  261106P06790000"

QUOTES = {
    HI_P: {"bid": 13.30, "ask": 13.50, "strike": 610.0, "put_call": "P"},
    LO_P: {"bid": 12.10, "ask": 12.30, "strike": 605.0, "put_call": "P"},
    LO_C: {"bid": 8.00, "ask": 8.20, "strike": 650.0, "put_call": "C"},
    HI_C: {"bid": 6.00, "ask": 6.20, "strike": 655.0, "put_call": "C"},
    SPX_HI: {"bid": 20.10, "ask": 20.50, "strike": 6800.0, "put_call": "P"},
    SPX_LO: {"bid": 17.80, "ask": 18.20, "strike": 6790.0, "put_call": "P"},
}


def quote(sym, max_age=1.0):
    q = QUOTES.get(sym)
    if not q:
        return {"symbol": sym, "found": False}
    return {"symbol": sym, "found": True, "asset_type": "OPTION", "multiplier": 100, "is_penny": True,
            "deliverables": None, **q}


def plan(**kw):
    base = {"underlying": "NFLX", "kind": "CREDIT", "near": HI_P, "far": LO_P, "entry": "mid", "qty": 1}
    return make_plan(quote, {**base, **kw})


# ---- plan --------------------------------------------------------------------------------------

def test_put_credit_plan_numbers():
    p = plan()
    assert p["ok"], p["errors"]
    assert [l["side"] for l in p["legs"]] == ["SHORT", "LONG"]
    assert p["mid"] == "1.20" and p["natural"] == "1.00"       # 13.30 - 12.30
    assert p["limit"] == "1.20" and p["width"] == "5.00"
    assert p["max_loss"] == "380.00" and p["max_gain"] == "120.00" and p["breakeven"] == "608.80"


def test_call_debit_plan_numbers():
    p = plan(kind="DEBIT", near=LO_C, far=HI_C)
    assert p["ok"], p["errors"]
    assert [l["side"] for l in p["legs"]] == ["LONG", "SHORT"]
    assert p["natural"] == "2.20"                              # 8.20 - 6.00
    assert p["limit"] == "2.00" and p["max_loss"] == "200.00" and p["max_gain"] == "300.00"
    assert p["breakeven"] == "652.00"


def test_the_far_strike_must_be_further_out():
    p = plan(near=LO_P, far=HI_P)
    assert not p["ok"] and "further out of the money" in p["errors"][0]


def test_index_spreads_use_nickels_and_round_toward_a_fill():
    p = make_plan(quote, {"underlying": "SPX", "kind": "CREDIT", "near": SPX_HI, "far": SPX_LO, "entry": "mid", "qty": 1})
    assert p["ok"], p["errors"]
    assert p["step"] == "0.05" and p["mid"] == "2.30" and p["limit"] == "2.30"
    p = make_plan(quote, {"underlying": "SPX", "kind": "CREDIT", "near": SPX_HI, "far": SPX_LO, "entry": "limit",
                          "limit_price": "2.32", "qty": 1})
    assert not p["ok"] and "0.05 steps" in p["errors"][0]


def test_credit_at_or_above_the_width_is_refused():
    p = plan(entry="limit", limit_price="5.00")
    assert not p["ok"] and "less than the strike width" in p["errors"][0]


def test_mixed_types_or_expiries_or_underlyings_are_refused():
    assert "calls, or both puts" in plan(far=HI_C)["errors"][0]
    assert not plan(underlying="AAPL")["ok"]


def test_grid_and_underlying_helpers():
    assert spread_step("SPX", {}, {}) == Decimal("0.05")
    assert spread_step("SPY", {}, {}) == Decimal("0.01")
    assert spread_step("NFLX", {"is_penny": True}, {"is_penny": False}) == Decimal("0.05")
    assert on_step(Decimal("1.21"), Decimal("0.05"), up=True) == Decimal("1.25")
    assert on_step(Decimal("1.24"), Decimal("0.05"), up=False) == Decimal("1.20")
    assert same_underlying(SPX_HI, "SPX") and same_underlying(SPX_HI, "$SPX") and not same_underlying(HI_P, "SPX")


def test_order_shapes_match_the_previewed_forms():
    legs = [{"symbol": HI_P, "side": "SHORT"}, {"symbol": LO_P, "side": "LONG"}]
    o = entry_order(legs, buy=False, price=Decimal("1.20"), qty=1)
    assert o["orderType"] == "NET_CREDIT" and o["complexOrderStrategyType"] == "VERTICAL" and o["duration"] == "DAY"
    assert [l["instruction"] for l in o["orderLegCollection"]] == ["SELL_TO_OPEN", "BUY_TO_OPEN"]
    c = close_order(legs, debit_spread=False, price=Decimal("0.60"), qty=1)
    assert c["orderType"] == "NET_DEBIT" and [l["instruction"] for l in c["orderLegCollection"]] == \
        ["BUY_TO_CLOSE", "SELL_TO_CLOSE"]
    c = close_order(legs, debit_spread=True, price=Decimal("0.60"), qty=1)
    assert c["orderType"] == "NET_CREDIT"


# ---- actions -----------------------------------------------------------------------------------

class Schwab:
    """Fake trading client: positions, orders, preview, place and cancel."""

    def __init__(self):
        self.positions, self.orders, self.placed, self.cancelled = {}, {}, [], []
        self.preview_resp, self.place_resp, self.place_exc = None, Resp(201, {"Location": ".../orders/700"}), None

    def account_details(self, _h, fields=None):
        pos = [{"instrument": {"symbol": s, "assetType": "OPTION"}, "longQuantity": max(q, 0), "shortQuantity": max(-q, 0)}
               for s, q in self.positions.items() if q]
        return JsonResp(200, {"securitiesAccount": {"positions": pos}})

    def account_orders(self, _h, start, end, maxResults=None, status=None):
        return JsonResp(200, [o for o in self.orders.values() if status is None or o.get("status") == status])

    def preview_order(self, _h, order):
        return self.preview_resp or JsonResp(200, {"orderValidationResult": {},
                                                   "orderStrategy": {"orderBalance": {"projectedBuyingPower": 1000.0}}})

    def place_order(self, _h, order):
        self.placed.append(order)
        if self.place_exc:
            raise self.place_exc
        return self.place_resp

    def order_details(self, _h, oid):
        return JsonResp(200, self.orders[oid])

    def cancel_order(self, _h, oid):
        self.cancelled.append(oid)
        self.orders[oid]["status"] = "CANCELED"
        return Resp(200)


@pytest.fixture
def desk(tmp_path, monkeypatch):
    t = Trader(Store(tmp_path / "d.db"))
    t._client, t._hash = Schwab(), "H" * 64
    t.quote = quote
    monkeypatch.setattr(spread_desk, "market_open", lambda now=None: True)
    monkeypatch.setattr(spread_desk.time, "sleep", lambda s: None)
    t.arm(True)
    d = SpreadDesk(t)
    d.sweep_fresh = lambda: True
    return d


def send(desk, **kw):
    p = plan()
    body = {"ticket_id": "spread-0001", "underlying": "NFLX", "kind": "CREDIT", "near": HI_P, "far": LO_P,
            "expected_limit": p["limit"], "expected_max_loss": p["max_loss"], "expected_qty": 1}
    return desk.submit({**body, **kw})


def test_submit_previews_then_places_one_vertical_and_reserves_both_legs(desk):
    r = send(desk)
    assert r["ok"] and r["order_id"] == 700
    placed = desk.t._client.placed
    assert len(placed) == 1 and placed[0]["price"] == "1.20" and placed[0]["orderType"] == "NET_CREDIT"
    sp = desk.book.get(r["spread_id"])
    assert sp["state"] == "PENDING" and sp["entry_order_id"] == 700 and sp["ticket"]["max_loss"] == 380.0
    assert desk.t._spread_gate([LO_P])                 # single-leg actions refuse both contracts now


def test_the_same_ticket_is_never_sent_twice(desk):
    send(desk)
    r = send(desk)
    assert r["duplicate"] and len(desk.t._client.placed) == 1


def test_disarmed_or_held_contracts_or_working_orders_send_nothing(desk):
    desk.t.arm(False)
    assert "not armed" in send(desk)["error"]
    desk.t.arm(True)
    desk.t._client.positions[LO_P] = 1
    assert "already holds" in send(desk)["error"]
    desk.t._client.positions = {}
    desk.t._client.orders[50] = {"orderId": 50, "status": "WORKING",
                                 "orderLegCollection": [{"instrument": {"symbol": HI_P}}]}
    assert "already touch" in send(desk)["error"]
    assert desk.t._client.placed == [] and desk.book.owner_of([HI_P]) is None


def test_a_preview_refusal_sends_nothing(desk):
    desk.t._client.preview_resp = Resp(400, text="Credit spreads cannot equal or exceed the strike difference")
    r = send(desk)
    assert not r["ok"] and "less than the strike width" in r["error"] and desk.t._client.placed == []


def test_a_changed_max_loss_sends_nothing(desk):
    r = send(desk, expected_max_loss="300.00")
    assert not r["ok"] and "Max loss is now" in r["error"] and desk.t._client.placed == []


def test_a_rejected_entry_releases_the_contracts(desk):
    desk.t._client.place_resp = Resp(400, text="nope")
    r = send(desk)
    assert r["status"] == "REJECTED" and desk.book.get(r["spread_id"])["state"] == "CANCELED"
    assert desk.book.owner_of([HI_P]) is None


def test_no_answer_keeps_the_contracts_and_blocks_the_next_spread(desk):
    desk.t._client.place_exc = TimeoutError()
    r = send(desk)
    assert r["status"] == "UNKNOWN" and desk.book.get(r["spread_id"])["state"] == "UNKNOWN"
    assert desk.book.owner_of([HI_P])
    desk.t._client.place_exc = None
    r2 = send(desk, ticket_id="spread-0002")
    assert "no confirmed order" in r2["error"] and len(desk.t._client.placed) == 1


def test_cancel_cancels_the_working_entry(desk):
    r = send(desk)
    desk.t._client.orders[700] = {"orderId": 700, "status": "WORKING", "filledQuantity": 0,
                                  "orderLegCollection": [{"instrument": {"symbol": HI_P}}, {"instrument": {"symbol": LO_P}}]}
    c = desk.cancel(r["spread_id"])
    assert c["ok"] and desk.t._client.cancelled == [700]


def filled_entry(n=1, oid=700, status="FILLED", qty=None, prices=(13.40, 12.20)):
    return {"orderId": oid, "status": status, "filledQuantity": n, "quantity": qty or n,
            "orderLegCollection": [{"legId": 1, "instrument": {"symbol": HI_P}}, {"legId": 2, "instrument": {"symbol": LO_P}}],
            "orderActivityCollection": [{"executionType": "FILL", "executionLegs": [
                {"legId": 1, "quantity": n, "price": prices[0]}, {"legId": 2, "quantity": n, "price": prices[1]}]}] if n else []}


def open_spread(desk, n=1):
    r = send(desk) if n == 1 else send(desk, expected_qty=n, expected_max_loss=f"{380 * n:.2f}")
    sid = r["spread_id"]
    desk.t._client.orders[700] = filled_entry(n)
    desk.t.store.save_orders([desk.t._client.orders[700]])
    desk.book._save_fills(sid, "entry", desk.t._client.orders[700])
    desk.book.set_state(sid, "OPEN", f"{n} spread(s) open")
    desk.t._client.positions = {HI_P: -n, LO_P: n}
    desk.t._client.place_resp = Resp(201, {"Location": ".../orders/800"})
    return sid


def test_close_sends_one_paired_net_debit(desk):
    sid = open_spread(desk)
    p = desk.close_plan(sid)
    assert p["ok"] and p["side"] == "buy" and p["limit"] == "1.20"
    r = desk.close({"spread_id": sid, "expected_limit": p["limit"]})
    assert r["ok"] and r["order_id"] == 800
    o = desk.t._client.placed[-1]
    assert o["orderType"] == "NET_DEBIT" and [l["instruction"] for l in o["orderLegCollection"]] == \
        ["BUY_TO_CLOSE", "SELL_TO_CLOSE"]
    assert desk.book.get(sid)["state"] == "CLOSING"


def test_close_refuses_when_the_account_disagrees(desk):
    sid = open_spread(desk)
    desk.t._client.positions = {HI_P: -1}
    r = desk.close({"spread_id": sid, "expected_limit": "1.20"})
    assert not r["ok"] and "account holds" in r["error"] and len(desk.t._client.placed) == 1


def test_an_unclear_close_holds_the_spread_and_is_never_resent(desk):
    sid = open_spread(desk)
    desk.t._client.place_exc = TimeoutError()
    r = desk.close({"spread_id": sid, "expected_limit": "1.20"})
    assert r["status"] == "UNKNOWN"
    sp = desk.book.get(sid)
    assert sp["state"] == "NEEDS_ACTION" and sp["hold"]
    # The evidence would say OPEN (the account still holds it), but the hold outranks it.
    desk.book.reconcile([], {HI_P: -1.0, LO_P: 1.0}, date(2026, 11, 3), True)
    assert desk.book.get(sid)["state"] == "NEEDS_ACTION"
    desk.t._client.place_exc = None
    assert "Only an OPEN spread" in desk.close({"spread_id": sid, "expected_limit": "1.20"})["error"]
    assert len(desk.t._client.placed) == 2          # the entry and the one unclear close


def test_resolve_clears_a_hold(desk):
    sid = open_spread(desk)
    desk.book.hold(sid, "close sent, no answer")
    ok, _ = desk.book.resolve(sid)
    assert ok and desk.book.get(sid)["state"] == "RESOLVED" and not desk.book.get(sid)["hold"]


# ---- audit 2026-11-03 fixes ----------------------------------------------------------------------

def test_crossed_or_delayed_leg_quotes_are_refused(monkeypatch):
    QUOTES[HI_P].update(bid=13.50, ask=13.30)
    try:
        assert "crossed" in " ".join(plan()["errors"])
    finally:
        QUOTES[HI_P].update(bid=13.30, ask=13.50)
    delayed = lambda sym, max_age=1.0: {**quote(sym), "realtime": False}
    assert "delayed" in " ".join(make_plan(delayed, {"underlying": "NFLX", "kind": "CREDIT", "near": HI_P,
                                                       "far": LO_P, "entry": "mid", "qty": 1})["errors"])


def test_no_entry_or_close_without_a_current_older_order_sweep(desk):
    desk.sweep_fresh = lambda: False
    r = send(desk)
    assert not r["ok"] and "59-day sweep" in r["error"] and desk.t._client.placed == []
    assert desk.book.owner_of([HI_P]) is None


def test_the_close_is_held_before_the_post_and_released_on_a_refusal(desk):
    sid = open_spread(desk)
    seen = {}
    client = desk.t._client
    orig = client.place_order

    def place(h, order):
        seen["hold"] = desk.book.get(sid)["hold"]
        return orig(h, order)
    client.place_order = place
    client.place_resp = Resp(400, text="nope")
    r = desk.close({"spread_id": sid, "expected_limit": "1.20"})
    assert seen["hold"] and r["status"] == "REJECTED"
    sp = desk.book.get(sid)
    assert sp["state"] == "OPEN" and not sp["hold"]
    client.place_resp = Resp(201, {"Location": ".../orders/801"})
    assert desk.close({"spread_id": sid, "expected_limit": "1.20"})["ok"]
    assert not desk.book.get(sid)["hold"] and desk.book.get(sid)["state"] == "CLOSING"


# ---- move a working close (2026-11-03, after the first live test) ---------------------------------

def closing_spread(desk):
    sid = open_spread(desk)
    r = desk.close({"spread_id": sid, "expected_limit": "1.20"})
    assert r["ok"] and r["order_id"] == 800
    desk.t._client.orders[800] = {"orderId": 800, "status": "WORKING", "filledQuantity": 0, "quantity": 1, "price": 1.20,
                                  "orderLegCollection": [{"instrument": {"symbol": HI_P}}, {"instrument": {"symbol": LO_P}}]}
    desk.t.store.save_orders([desk.t._client.orders[800]])
    desk.t._client.place_resp = Resp(201, {"Location": ".../orders/801"})
    return sid


def test_moving_a_working_close_cancels_it_first_then_sends_one_new_close(desk):
    sid = closing_spread(desk)
    r = desk.close({"spread_id": sid, "expected_limit": "1.25", "reprice": True})
    assert r["ok"] and r["order_id"] == 801
    assert desk.t._client.cancelled == [800]
    assert [o["price"] for o in desk.t._client.placed[1:]] == ["1.20", "1.25"]
    assert desk.book.get(sid)["close_order_id"] == 801 and desk.book.get(sid)["state"] == "CLOSING"


def test_a_close_that_filled_before_the_move_is_not_replaced(desk):
    sid = closing_spread(desk)
    desk.t._client.orders[800].update(status="FILLED", filledQuantity=1)
    r = desk.close({"spread_id": sid, "expected_limit": "1.25", "reprice": True})
    assert not r["ok"] and "not replaced" in r["error"] and len(desk.t._client.placed) == 2


def test_a_closing_spread_is_not_closed_again_without_reprice(desk):
    sid = closing_spread(desk)
    assert "Only an OPEN spread" in desk.close({"spread_id": sid, "expected_limit": "1.25"})["error"]
    assert desk.t._client.cancelled == []


def test_the_close_plan_shows_the_working_close_and_its_price(desk):
    sid = closing_spread(desk)
    p = desk.close_plan(sid, "natural")
    assert p["ok"] and p["limit"] == "1.40"                    # buy back at natural: 13.50 - 12.10
    assert p["working"] == [{"role": "close", "order_id": 800, "status": "WORKING", "price": 1.20, "filled": 0,
                             "quantity": 1, "type": None, "duration": None}]


def test_the_close_price_on_screen_costs_no_quote_requests_when_the_poller_has_the_legs(desk):
    sid = open_spread(desk)
    desk.held_quotes = lambda sym: {**quote(sym), "realtime": True}
    def no_requests(sym, max_age=1.0):
        raise AssertionError("a display refresh asked Schwab for a quote")
    desk.t.quote = no_requests
    assert desk.close_plan(sid, "natural")["limit"] == "1.40"


# ---- polish after the BA live test --------------------------------------------------------------

def test_a_close_just_sent_is_not_unknown_while_schwab_lists_it(desk):
    sid = open_spread(desk)
    filled = {"orderId": 700, "status": "FILLED", "filledQuantity": 1,
              "orderLegCollection": [{"legId": 1, "instrument": {"symbol": HI_P}}, {"legId": 2, "instrument": {"symbol": LO_P}}],
              "orderActivityCollection": [{"executionType": "FILL", "executionLegs": [{"legId": 1, "quantity": 1},
                                                                                      {"legId": 2, "quantity": 1}]}]}
    assert desk.close({"spread_id": sid, "expected_limit": "1.20"})["ok"]      # close 800, not listed yet
    desk.book.reconcile([filled], {HI_P: -1.0, LO_P: 1.0}, date(2026, 11, 3), True)
    assert desk.book.get(sid)["state"] == "CLOSING"


def test_closed_spreads_are_listed_for_the_day(desk):
    sid = open_spread(desk)
    desk.book.attach_order(sid, "close", 800)
    desk.book.set_state(sid, "CLOSED", "closed by its own close order")
    assert [x["spread_id"] for x in desk.book.closed_since("2000-01-01T00:00:00+00:00")] == [sid]
    assert desk.book.closed_since("2999-01-01T00:00:00+00:00") == []


# ---- quantity and Risk $ sizing (slice 5) --------------------------------------------------------

def test_risk_sizing_buys_as_many_spreads_as_the_risk_covers():
    p = plan(size_mode="risk", risk=1000)                # 380 max loss each
    assert p["ok"] and p["qty"] == 2 and p["max_loss"] == "760.00" and p["max_loss_each"] == "380.00"
    assert "less than one spread" in plan(size_mode="risk", risk=300)["errors"][0]
    assert "at most 20" in plan(qty=21)["errors"][0]


def test_the_entry_order_carries_the_displayed_quantity(desk):
    r = send(desk, expected_qty=3, expected_max_loss="1140.00")
    assert r["ok"]
    o = desk.t._client.placed[0]
    assert o["quantity"] == 3 and [l["quantity"] for l in o["orderLegCollection"]] == [3, 3]
    assert desk.book.get(r["spread_id"])["qty"] == 3


def test_a_quantity_that_no_longer_matches_the_max_loss_sends_nothing(desk):
    r = send(desk, expected_qty=3, expected_max_loss="380.00")
    assert not r["ok"] and desk.t._client.placed == []


def test_cancel_after_a_partial_fill_cancels_the_rest_and_close_closes_what_filled(desk):
    r = send(desk, expected_qty=3, expected_max_loss="1140.00")
    sid = r["spread_id"]
    desk.t._client.orders[700] = filled_entry(1, status="WORKING", qty=3)
    c = desk.cancel(sid)
    assert c["ok"] and "1 of 3 had filled" in c["message"]
    assert desk.book.held_units(desk.book.get(sid)) == 1
    desk.book.set_state(sid, "OPEN", "1 spread(s) open")
    desk.t._client.positions = {HI_P: -1, LO_P: 1}
    desk.t._client.place_resp = Resp(201, {"Location": ".../orders/800"})
    r2 = desk.close({"spread_id": sid, "expected_limit": "1.20"})
    assert r2["ok"] and r2["qty"] == 1
    assert [l["quantity"] for l in desk.t._client.placed[-1]["orderLegCollection"]] == [1, 1]


def test_moving_a_partly_filled_close_closes_only_the_rest(desk):
    sid = open_spread(desk, n=3)
    assert desk.close({"spread_id": sid, "expected_limit": "1.20"})["ok"]
    part = filled_entry(2, oid=800, status="WORKING", qty=3)
    part["orderLegCollection"][0]["instruction"] = "BUY_TO_CLOSE"
    desk.t._client.orders[800] = part
    desk.t._client.positions = {HI_P: -1, LO_P: 1}
    desk.t._client.place_resp = Resp(201, {"Location": ".../orders/801"})
    r = desk.close({"spread_id": sid, "expected_limit": "1.25", "reprice": True})
    assert r["ok"] and r["qty"] == 1 and desk.t._client.cancelled == [800]


# ---- GTC profit target (slice 3) -----------------------------------------------------------------

def target_order(oid=800, status="WORKING", filled=0, qty=1, price=0.60):
    o = filled_entry(filled, oid=oid, status=status, qty=qty)
    o.update(price=price, duration="GOOD_TILL_CANCEL", orderType="NET_DEBIT")
    return o


def targeted_spread(desk, n=1):
    sid = open_spread(desk, n)
    r = desk.set_target({"spread_id": sid, "expected_limit": "0.60"})
    assert r["ok"] and r["order_id"] == 800
    desk.t._client.orders[800] = target_order(qty=n)
    desk.t.store.save_orders([desk.t._client.orders[800]])
    desk.t._client.place_resp = Resp(201, {"Location": ".../orders/801"})
    return sid


def test_target_price_is_a_share_of_the_max_gain_rounded_in_your_favor(desk):
    sid = open_spread(desk)                                   # filled 13.40 - 12.20 = 1.20 credit
    p = desk.target_plan(sid, pct="0.5")
    assert p["ok"] and p["basis"] == "fill" and p["limit"] == "0.60" and p["gross"] == "60.00"
    assert p["pct"] == 0.5 and round(p["r"], 4) == round(0.60 / 3.80, 4)
    assert desk.target_plan(sid, pct="0.25")["limit"] == "0.90"
    # A 1.25 credit: half the gain is a 0.625 buy back, rounded DOWN to 0.62.
    desk.t.store.save_orders([filled_entry(1, prices=(13.40, 12.15))])
    p = desk.target_plan(sid, pct="0.5")
    assert p["basis"] == "fill" and p["entry"] == "1.25" and p["limit"] == "0.62"


def test_a_percent_target_waits_for_the_fill_prices(desk):
    sid = open_spread(desk)
    part = filled_entry(1)
    part["orderActivityCollection"][0]["executionLegs"][1].pop("price")       # one leg's price not listed yet
    desk.t.store.save_orders([part])
    p = desk.target_plan(sid, pct="0.5")
    assert not p["ok"] and "fill prices" in p["errors"][0]
    assert desk.target_plan(sid, price="0.60")["ok"]           # a typed price still works (shown vs the limit)


def test_a_debit_spread_target_sells_above_the_entry_rounded_up(desk):
    sid = open_spread(desk)
    with desk.t.store._lock, desk.t.store._con:
        desk.t.store._con.execute("update spreads set kind = 'CALL_DEBIT' where spread_id = ?", (sid,))
        desk.t.store._con.execute("update spread_legs set side = case side when 'SHORT' then 'LONG' else 'SHORT' end "
                                  "where spread_id = ?", (sid,))
    desk.t.store.save_orders([filled_entry(1, prices=(13.41, 12.20))])   # bought 13.41, sold 12.20: 1.21 debit
    p = desk.target_plan(sid, pct="0.5")                      # gain 3.79: 1.21 + 1.895 = 3.105, up to 3.11
    assert p["ok"] and p["side"] == "sell" and p["limit"] == "3.11"


def test_a_target_must_be_on_the_grid_and_on_the_profit_side(desk):
    sid = open_spread(desk)
    assert "below the entry" in desk.target_plan(sid, price="1.30")["errors"][0]
    desk.held_quotes = lambda sym: {**quote(sym), "is_penny": False}
    assert "0.05 steps" in desk.target_plan(sid, price="0.62")["errors"][0]
    assert "% of the max gain" in desk.target_plan(sid, pct="1")["errors"][0]


def test_set_target_sends_one_gtc_paired_close_of_what_is_held(desk):
    sid = targeted_spread(desk, n=2)
    o = desk.t._client.placed[-1]
    assert o["duration"] == "GOOD_TILL_CANCEL" and o["orderType"] == "NET_DEBIT" and o["price"] == "0.60"
    assert [l["quantity"] for l in o["orderLegCollection"]] == [2, 2]
    sp = desk.book.get(sid)
    assert sp["target_order_id"] == 800 and sp["state"] == "OPEN" and sp["detail"] == "target sent at 0.60"
    assert desk.close_plan(sid)["working"][0]["role"] == "target"


def test_moving_a_target_cancels_it_first_even_when_the_sweep_still_lists_it(desk):
    sid = targeted_spread(desk)
    stale = dict(desk.t._client.orders[800])                  # entered on an earlier day: only the sweep has it
    desk.snapshot = lambda: [stale]
    client = desk.t._client
    client.account_orders = lambda *a, **k: JsonResp(200, [])
    r = desk.set_target({"spread_id": sid, "expected_limit": "0.50"})
    assert r["ok"] and r["moved"] and r["order_id"] == 801 and client.cancelled == [800]
    assert client.placed[-1]["price"] == "0.50" and desk.book.get(sid)["target_order_id"] == 801


def test_a_rejected_move_says_the_old_target_is_gone(desk):
    sid = targeted_spread(desk)
    desk.t._client.place_resp = Resp(400, text="nope")
    r = desk.set_target({"spread_id": sid, "expected_limit": "0.50"})
    assert not r["ok"] and "old target was cancelled" in r["error"] and desk.book.get(sid)["state"] == "OPEN"


def test_close_cancels_the_target_first_then_closes(desk):
    sid = targeted_spread(desk)
    r = desk.close({"spread_id": sid, "expected_limit": "1.20"})
    assert r["ok"] and r["order_id"] == 801 and desk.t._client.cancelled == [800]
    o = desk.t._client.placed[-1]
    assert o["duration"] == "DAY" and o["price"] == "1.20"
    assert desk.book.get(sid)["state"] == "CLOSING"


def test_close_after_the_target_filled_sends_nothing(desk):
    sid = targeted_spread(desk)
    desk.t._client.orders[800] = target_order(status="FILLED", filled=1)
    r = desk.close({"spread_id": sid, "expected_limit": "1.20"})
    assert not r["ok"] and "target filled" in r["error"] and len(desk.t._client.placed) == 2


def test_close_after_a_partial_target_closes_only_the_rest(desk):
    sid = targeted_spread(desk, n=3)
    desk.t._client.orders[800] = target_order(filled=2, qty=3)
    desk.t._client.positions = {HI_P: -1, LO_P: 1}
    r = desk.close({"spread_id": sid, "expected_limit": "1.20"})
    assert r["ok"] and r["qty"] == 1 and desk.t._client.cancelled == [800]


def test_an_unclear_target_holds_the_spread_and_is_never_resent(desk):
    sid = open_spread(desk)
    desk.t._client.place_exc = TimeoutError()
    r = desk.set_target({"spread_id": sid, "expected_limit": "0.60"})
    assert r["status"] == "UNKNOWN" and desk.book.get(sid)["state"] == "NEEDS_ACTION"
    desk.t._client.place_exc = None
    assert "OPEN spread" in desk.set_target({"spread_id": sid, "expected_limit": "0.60"})["error"]
    assert "OPEN spread" in desk.close({"spread_id": sid, "expected_limit": "1.20"})["error"]
    assert len(desk.t._client.placed) == 2


def test_cancel_target_cancels_only_the_target(desk):
    sid = targeted_spread(desk)
    c = desk.cancel_target(sid)
    assert c["ok"] and desk.t._client.cancelled == [800] and desk.book.get(sid)["state"] == "OPEN"


def test_a_target_fill_closes_the_spread_and_a_working_one_keeps_it_open(desk):
    sid = targeted_spread(desk)
    today = date(2026, 11, 4)
    desk.book.reconcile([filled_entry(1), target_order()], {HI_P: -1.0, LO_P: 1.0}, today, True)
    sp = desk.book.get(sid)
    assert sp["state"] == "OPEN" and "target working at 0.60" in sp["detail"]
    desk.book.reconcile([filled_entry(1), target_order(status="FILLED", filled=1)], {}, today, True)
    sp = desk.book.get(sid)
    assert sp["state"] == "CLOSED" and sp["detail"] == "closed by its own target"
    assert desk.book.owner_of([HI_P]) is None


def test_a_close_refused_after_cancelling_the_target_says_the_target_is_gone(desk):
    sid = targeted_spread(desk)
    desk.t._client.preview_resp = Resp(400, text="nope")
    r = desk.close({"spread_id": sid, "expected_limit": "1.20"})
    assert not r["ok"] and "target was cancelled first" in r["error"] and len(desk.t._client.placed) == 2


def test_a_target_whose_fills_are_not_all_listed_blocks_the_close(desk):
    sid = targeted_spread(desk, n=3)
    lag = target_order(status="CANCELED", filled=0, qty=3)
    lag["filledQuantity"] = 2                                 # Schwab's count is ahead of its execution legs
    desk.t._client.orders[800] = lag
    r = desk.close({"spread_id": sid, "expected_limit": "1.20"})
    assert not r["ok"] and "not listed all its executions" in r["error"] and len(desk.t._client.placed) == 2


def test_a_target_replaced_in_thinkorswim_is_not_treated_as_gone(desk):
    sid = targeted_spread(desk)
    desk.t._client.orders[800]["status"] = "REPLACED"
    r = desk.close({"spread_id": sid, "expected_limit": "1.20"})
    assert not r["ok"] and "replaced outside the companion" in r["error"] and len(desk.t._client.placed) == 2
    r = desk.set_target({"spread_id": sid, "expected_limit": "0.50"})
    assert not r["ok"] and "replaced outside the companion" in r["error"] and len(desk.t._client.placed) == 2


# ---- watch-only stop (slice 4) -------------------------------------------------------------------

def test_stop_plan_is_a_share_of_the_max_loss_rounded_tighter(desk):
    sid = open_spread(desk)                                   # 1.20 credit, 5 wide: 1R = 3.80
    p = desk.stop_plan(sid, r="0.5")
    assert p["ok"] and p["limit"] == "3.10" and p["r"] == 0.5 and p["gross"] == "-190.00"
    desk.held_quotes = lambda sym: {**quote(sym), "is_penny": False}
    assert desk.stop_plan(sid, r="0.33")["limit"] == "2.45"   # 1.20 + 1.254 = 2.454: down to the nickel
    assert "above the entry" in desk.stop_plan(sid, price="1.00")["errors"][0]


def test_set_stop_watches_only_and_sends_nothing(desk):
    sid = open_spread(desk)
    placed = len(desk.t._client.placed)
    r = desk.set_stop({"spread_id": sid, "expected_price": "3.10"})
    assert r["ok"] and desk.book.stops.get(sid)["price"] == 3.10
    assert len(desk.t._client.placed) == placed and desk.t._client.cancelled == []


# ---- partial close and Send when hit (2026-11-04) -----------------------------------------------

def test_a_partial_close_closes_part_and_keeps_the_rest_open(desk):
    sid = open_spread(desk, n=3)
    r = desk.close({"spread_id": sid, "expected_limit": "1.20", "qty": 1})
    assert r["ok"] and r["qty"] == 1 and r["of"] == 3
    assert [l["quantity"] for l in desk.t._client.placed[-1]["orderLegCollection"]] == [1, 1]
    assert desk.book.get(sid)["state"] == "CLOSING"


def test_a_close_size_outside_what_is_held_is_refused(desk):
    sid = open_spread(desk, n=3)
    for q in (0, 4, "x"):
        assert "Close 1 to 3" in desk.close({"spread_id": sid, "expected_limit": "1.20", "qty": q})["error"]
    assert len(desk.t._client.placed) == 1


def test_a_fire_with_send_on_calls_the_desk_once(desk):
    sid = open_spread(desk)
    calls = []
    desk.book.on_fire = calls.append
    desk.book.stops.watch = lambda sp, q, m: {"status": "fired", "fired_now": True, "send": True}
    desk.book.watch_stops([{"spread_id": sid}], {}, True)
    desk.book.stops.watch = lambda sp, q, m: {"status": "fired", "send": True}          # the next rebuilds
    desk.book.watch_stops([{"spread_id": sid}], {}, True)
    desk.book.stops.watch = lambda sp, q, m: {"status": "fired", "fired_now": True, "send": False}
    desk.book.watch_stops([{"spread_id": sid}], {}, True)
    assert calls == [sid]


def test_a_partial_close_puts_the_target_back_on_the_rest(desk):
    sid = targeted_spread(desk, n=3)                          # target 800: 3 at 0.60
    desk.t._client.place_resp = Resp(201, {"Location": ".../orders/801"})
    placed = desk.t._client.placed
    orig = desk.t._client.place_order
    ids = iter([801, 802])
    def place(_h, order):
        placed.append(order)
        return Resp(201, {"Location": f".../orders/{next(ids)}"})
    desk.t._client.place_order = place
    r = desk.close({"spread_id": sid, "expected_limit": "1.20", "qty": 1})
    assert r["ok"] and r["qty"] == 1 and desk.t._client.cancelled == [800]
    close, target = placed[-2], placed[-1]
    assert close["duration"] == "DAY" and [l["quantity"] for l in close["orderLegCollection"]] == [1, 1]
    assert target["duration"] == "GOOD_TILL_CANCEL" and target["price"] == "0.60" \
        and [l["quantity"] for l in target["orderLegCollection"]] == [2, 2]
    sp = desk.book.get(sid)
    assert sp["close_order_id"] == 801 and sp["target_order_id"] == 802 and sp["state"] == "CLOSING"


def test_moving_a_partial_close_keeps_its_size_and_the_target(desk):
    sid = targeted_spread(desk, n=3)
    desk.t._client.orders[800] = target_order(qty=2)          # the target already on the other 2
    desk.t._client.place_resp = Resp(201, {"Location": ".../orders/801"})
    assert desk.close({"spread_id": sid, "expected_limit": "1.20", "qty": 1})["ok"]
    desk.t._client.orders[801] = {**filled_entry(0, oid=801, status="WORKING", qty=1), "price": 1.20}
    desk.t._client.place_resp = Resp(201, {"Location": ".../orders/802"})
    r = desk.close({"spread_id": sid, "expected_limit": "1.25", "reprice": True})
    assert r["ok"] and r["qty"] == 1 and desk.t._client.cancelled == [801]      # the target 800 keeps working


def test_move_target_to_the_same_price_does_nothing(desk):
    sid = targeted_spread(desk)
    r = desk.set_target({"spread_id": sid, "expected_limit": "0.60"})
    assert not r["ok"] and "already works at 0.60" in r["error"] and desk.t._client.cancelled == []


def fired_stop(desk, sid, send=True):
    desk.book.stops.set(sid, 1.40, 0.05, send=send)
    with desk.t.store._lock, desk.t.store._con:
        desk.t.store._con.execute("update spread_stops set fired_at = '2026-11-04T19:00:00+00:00', fired_mid = 1.41")


def test_send_when_hit_sends_one_close_of_all_at_natural_plus_two_steps(desk):
    sid = open_spread(desk, n=2)
    fired_stop(desk, sid)
    r = desk.auto_stop(sid)
    assert r["ok"] and r["qty"] == 2
    o = desk.t._client.placed[-1]
    assert o["price"] == "1.42" and o["duration"] == "DAY"         # natural 13.50 - 12.10 = 1.40, + 2 pennies
    st = desk.book.stops.get(sid)
    assert st["sent_at"] and st["sent_result"].startswith("sent: close order 800")
    assert not desk.auto_stop(sid)["ok"] and len(desk.t._client.placed) == 2     # never twice


def test_send_when_hit_cancels_the_target_first(desk):
    sid = targeted_spread(desk, n=2)
    fired_stop(desk, sid)
    r = desk.auto_stop(sid)
    assert r["ok"] and r["qty"] == 2 and desk.t._client.cancelled == [800]


def test_a_refused_stop_send_is_recorded_and_never_retried(desk):
    sid = open_spread(desk)
    fired_stop(desk, sid)
    desk.t._client.preview_resp = Resp(400, text="nope")
    r = desk.auto_stop(sid)
    assert not r["ok"] and desk.book.stops.get(sid)["sent_result"].startswith("refused")
    desk.t._client.preview_resp = None
    assert not desk.auto_stop(sid)["ok"] and len(desk.t._client.placed) == 1


def test_a_watch_only_stop_never_sends(desk):
    sid = open_spread(desk)
    fired_stop(desk, sid, send=False)
    assert not desk.auto_stop(sid)["ok"] and len(desk.t._client.placed) == 1


def test_the_stop_close_never_pays_past_the_width(desk, monkeypatch):
    sid = open_spread(desk)
    fired_stop(desk, sid)
    monkeypatch.setattr(desk, "close_plan", lambda *a, **k: {"ok": True, "natural": "4.99", "step": "0.01",
                                                             "width": "5.00", "limit": "4.99", "errors": []})
    seen = {}
    monkeypatch.setattr(desk, "_close_locked", lambda sp, req, sent: seen.update(req) or {"ok": True, "order_id": 1,
                                                                                         "qty": 1, "limit": req["expected_limit"]})
    desk.auto_stop(sid)
    assert seen["expected_limit"] == "5.00" and seen["qty"] == "all"



def test_an_unclear_stop_send_never_says_close_it_yourself(desk):
    sid = open_spread(desk)
    fired_stop(desk, sid)
    desk.t._client.place_exc = TimeoutError()
    desk.auto_stop(sid)
    res = desk.book.stops.get(sid)["sent_result"]
    assert res.startswith("unclear") and "do NOT close again" in res and desk.book.get(sid)["state"] == "NEEDS_ACTION"


def test_a_stop_after_the_target_filled_reads_closed(desk):
    sid = targeted_spread(desk)
    fired_stop(desk, sid)
    desk.t._client.orders[800] = target_order(status="FILLED", filled=1)
    desk.auto_stop(sid)
    assert desk.book.stops.get(sid)["sent_result"].startswith("closed")


def test_a_refused_stop_send_says_the_exits_it_already_cancelled(desk):
    sid = targeted_spread(desk)
    fired_stop(desk, sid)
    desk.t._client.preview_resp = Resp(400, text="nope")
    desk.auto_stop(sid)
    res = desk.book.stops.get(sid)["sent_result"]
    assert res.startswith("refused") and "the target had been cancelled" in res


def test_the_stop_on_a_closing_spread_with_a_target_moves_the_close_and_closes_all(desk):
    sid = targeted_spread(desk, n=3)
    desk.t._client.orders[800] = target_order(qty=2)
    assert desk.close({"spread_id": sid, "expected_limit": "1.20", "qty": 1})["ok"]      # close 801 for 1, target on 2
    desk.t._client.orders[801] = {**filled_entry(0, oid=801, status="WORKING", qty=1), "price": 1.20}
    desk.t._client.place_resp = Resp(201, {"Location": ".../orders/802"})
    fired_stop(desk, sid)
    r = desk.auto_stop(sid)
    assert r["ok"] and r["qty"] == 3 and sorted(desk.t._client.cancelled) == [800, 801]


def test_cancel_target_never_makes_a_closing_spread_look_open(desk):
    sid = targeted_spread(desk, n=3)
    desk.t._client.orders[800] = target_order(qty=2)
    assert desk.close({"spread_id": sid, "expected_limit": "1.20", "qty": 1})["ok"]
    assert desk.cancel_target(sid)["ok"] and desk.book.get(sid)["state"] == "CLOSING"


def test_a_restart_turns_an_interrupted_send_into_unclear(desk):
    from companion.spreads.spread_stop import StopWatch
    sid = open_spread(desk)
    fired_stop(desk, sid)
    assert desk.book.stops.claim_send(sid)
    res = StopWatch(desk.t.store).get(sid)["sent_result"]
    assert res.startswith("unclear") and "stopped while sending" in res
