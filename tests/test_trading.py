"""Order ticket service with a fake Schwab client: nothing here talks to Schwab."""
from decimal import Decimal

import pytest

from companion import trading
from companion.store import Store
from companion.trading import Trader


class Resp:
    def __init__(self, status=201, headers=None, text=""):
        self.status_code, self.headers, self.text, self.content = status, headers or {}, text, b""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, resp=None, exc=None):
        self.placed, self.resp, self.exc = [], resp or Resp(201, {"Location": ".../orders/555"}), exc

    def place_order(self, _hash, order):
        self.placed.append(order)
        if self.exc:
            raise self.exc
        return self.resp


@pytest.fixture
def trader(tmp_path, monkeypatch):
    t = Trader(Store(tmp_path / "t.db"))
    t._client, t._hash = FakeClient(), "H" * 64
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    monkeypatch.setattr(t, "quote", lambda sym, max_age=1.0: {"bid": 338.70, "ask": 338.77})
    t.arm(True)
    return t


def req(**kw):
    base = {"ticket_id": "ticket-0001", "symbol": "BRVO", "side": "LONG", "stop": "336.00",
            "risk": 500, "r": 2, "expected_limit": "338.94", "expected_qty": 170,
            "expected_target": "344.82"}
    return {**base, **kw}


def test_places_exactly_the_displayed_bracket(trader):
    r = trader.submit(req())
    assert r["ok"] and r["order_id"] == 555
    (order,) = trader._client.placed
    assert order["orderLegCollection"][0]["quantity"] == 170 and order["price"] == "338.94"


def test_two_target_ticket_sends_ot2oco_and_reconciles(trader):
    r = trader.submit(req(split=0.5, t1_r=1, expected_qty1=85, expected_target1="341.88"))
    assert r["ok"]
    (order,) = trader._client.placed
    assert [len(c["childOrderStrategies"]) for c in order["childOrderStrategies"]] == [2, 2]
    # A displayed split that no longer matches is refused.
    r = trader.submit(req(ticket_id="ticket-0002", split=0.5, t1_r=1, expected_qty1=80,
                          expected_target1="341.88"))
    assert not r["ok"] and "Split changed" in r["error"]
    # Reconcile matches the exact two-OCO structure that was sent.
    t = trader.store.get_ticket("ticket-0001")
    trader.store.update_ticket("ticket-0001", "UNKNOWN")
    live = {**order, "orderId": 77, "quantity": 170, "status": "WORKING",
            "enteredTime": t["created"][:19] + "+0000"}
    trader.reconcile([live])
    assert trader.store.get_ticket("ticket-0001")["order_id"] == 77


def test_same_ticket_twice_never_sends_twice(trader):
    trader.submit(req())
    r = trader.submit(req())
    assert r["duplicate"] and len(trader._client.placed) == 1


def test_arming_lapses_at_the_close(trader):
    trader.store.set_setting("trading_armed_at", "2026-10-19T14:00:00+00:00")   # 10:00 ET yesterday
    assert not trader.armed
    assert trader.store.get_setting("trading_armed") == "0"
    assert "DISARM (session close)" in trader.store.get_setting("trading_arm_log")


def test_not_armed_sends_nothing(trader):
    trader.arm(False)
    assert not trader.submit(req())["ok"] and trader._client.placed == []


def test_stale_ticket_is_refused(trader):
    r = trader.submit(req(expected_qty=200))         # displayed more shares than the fresh plan allows
    assert not r["ok"] and "Size changed" in r["error"] and trader._client.placed == []
    r = trader.submit(req(ticket_id="ticket-0002", expected_limit="337.00"))
    assert not r["ok"] and "Price moved" in r["error"] and trader._client.placed == []


def _quote(monkeypatch, trader, bid, ask):
    monkeypatch.setattr(trader, "quote", lambda sym, max_age=1.0: {"bid": bid, "ask": ask})


def test_small_move_against_sends_the_locked_plan(trader, monkeypatch):
    # Ticket showed 170 @ 338.94 (1R = 2.94). The ask ticked up 18c (under 0.1R = 0.294): a fresh
    # plan would size 160 shares, but the click sends exactly what was displayed.
    _quote(monkeypatch, trader, 338.88, 338.95)
    r = trader.submit(req())
    assert r["ok"], r
    (order,) = trader._client.placed
    assert order["orderLegCollection"][0]["quantity"] == 170 and order["price"] == "338.94"
    oco = order["childOrderStrategies"][0]["childOrderStrategies"]
    assert {c.get("price") or c.get("stopPrice") for c in oco} == {"344.82", "336.00"}


def test_move_against_beyond_the_lock_is_refused(trader, monkeypatch):
    _quote(monkeypatch, trader, 339.20, 339.30)          # entry would now be 339.47: 0.53 > 0.294
    r = trader.submit(req())
    assert not r["ok"] and "Price moved" in r["error"] and trader._client.placed == []
    assert r["plan"]["limit"] == "339.47"                # the ticket gets the new numbers


def test_move_in_favour_keeps_the_displayed_plan(trader, monkeypatch):
    _quote(monkeypatch, trader, 337.40, 337.50)          # long, price dropped: fills at or below 338.94
    r = trader.submit(req())
    assert r["ok"] and trader._client.placed[0]["price"] == "338.94"


def test_stop_through_the_market_is_refused(trader, monkeypatch):
    _quote(monkeypatch, trader, 335.90, 336.05)          # bid is now below the 336.00 stop
    r = trader.submit(req())
    assert not r["ok"] and "below the bid" in r["error"] and trader._client.placed == []


def test_market_entry_keeps_the_displayed_size(trader, monkeypatch):
    _quote(monkeypatch, trader, 338.88, 338.95)
    r = trader.submit(req(entry="market"))
    assert r["ok"]
    (order,) = trader._client.placed
    assert order["orderType"] == "MARKET" and order["orderLegCollection"][0]["quantity"] == 170


def test_lock_is_at_least_two_ticks(trader, monkeypatch):
    # Ticket showed 500 @ 338.94 at the ask with a 10c stop: 0.1R = 0.01, so the 2-tick floor applies.
    base = dict(entry="ask", stop="338.84", risk=50, expected_qty=500, expected_target="339.14")
    _quote(monkeypatch, trader, 338.90, 338.96)          # ask 2c higher: inside the floor
    r = trader.submit(req(**base))
    assert r["ok"], r
    assert trader._client.placed[0]["price"] == "338.94"
    _quote(monkeypatch, trader, 338.90, 338.97)          # 3c higher: refused
    r = trader.submit(req(ticket_id="ticket-0002", **base))
    assert not r["ok"] and "Price moved" in r["error"]


def test_short_lock_mirrors_the_long(trader, monkeypatch):
    # Short 202 @ 338.53 (bid 338.70 less 0.05%), stop 341.00: 1R = 2.47, lock 0.247.
    base = dict(side="SHORT", stop="341.00", expected_limit="338.53", expected_qty=202,
                expected_target="333.59")
    _quote(monkeypatch, trader, 338.50, 338.57)          # entry would be 338.33: 0.20 against
    r = trader.submit(req(**base))
    assert r["ok"], r
    assert trader._client.placed[0]["price"] == "338.53"
    _quote(monkeypatch, trader, 338.40, 338.47)          # 338.23: 0.30 against, refused
    r = trader.submit(req(ticket_id="ticket-0002", **base))
    assert not r["ok"] and "Price moved" in r["error"]


def test_favourable_move_into_the_stop_is_refused(trader, monkeypatch):
    # Long at the bid 338.70, stop 336.00. Price fell to one tick above the stop.
    base = dict(entry="bid", expected_limit="338.70", expected_qty=185, expected_target="344.10")
    _quote(monkeypatch, trader, 336.01, 336.02)
    r = trader.submit(req(**base))
    assert not r["ok"] and "within 2 ticks of the stop" in r["error"] and trader._client.placed == []


def test_trail_that_would_start_looser_is_refused(trader, monkeypatch):
    # A 1R trail on the mark showed a start of 335.80 at 338.70 / 338.77.
    base = dict(stop_type="trail", expected_trail_offset=2.94, expected_trail_start="335.80")
    r = trader.submit(req(**base))
    assert r["ok"], r
    _quote(monkeypatch, trader, 337.50, 338.77)          # spread widened: the mark drops, start 335.20
    r = trader.submit(req(ticket_id="ticket-0002", **base))
    assert not r["ok"] and "trail would now start at 335.20" in r["error"]
    r = trader.submit(req(ticket_id="ticket-0003", stop_type="trail", expected_trail_offset=2.94))
    assert not r["ok"] and "reload" in r["error"]


def test_changed_target_is_refused(trader):
    r = trader.submit(req(expected_target="344.80"))
    assert not r["ok"] and "Target is now 344.82" in r["error"] and trader._client.placed == []


def test_fewer_shares_than_fresh_plan_sends_the_displayed_size(trader):
    r = trader.submit(req(expected_qty=100))
    assert r["ok"] and trader._client.placed[0]["orderLegCollection"][0]["quantity"] == 100


def test_timeout_is_unknown_and_blocks_new_tickets(trader):
    trader._client.exc = TimeoutError("read timed out")
    r = trader.submit(req())
    assert r["status"] == "UNKNOWN"
    trader._client.exc = None
    r = trader.submit(req(ticket_id="ticket-0002"))
    assert not r["ok"] and "not confirmed" in r["error"] and len(trader._client.placed) == 1
    trader.resolve("ticket-0001")
    assert trader.submit(req(ticket_id="ticket-0002"))["ok"]


def test_error_after_sending_is_reported_as_unknown(trader):
    trader._client.resp = None   # place_order "succeeds" but handling the answer then fails
    r = trader.submit(req())
    assert r["status"] == "UNKNOWN" and "MAY be live" in r["error"]
    assert trader.store.get_ticket("ticket-0001")["status"] == "UNKNOWN"


def test_429_is_throttled_not_rejected_and_retry_is_allowed(trader):
    trader._client.resp = Resp(429, {"Retry-After": "2"}, text='{"title":"Too many requests"}')
    r = trader.submit(req())
    assert r["status"] == "RATE_LIMITED" and r["retry_after"] == "2" and "nothing was placed" in r["error"]
    assert trader.store.get_ticket("ticket-0001")["status"] == "RATE_LIMITED"
    trader._client.resp = Resp(201, {"Location": ".../orders/557"})
    assert trader.submit(req(ticket_id="ticket-0002"))["ok"]       # not blocked


def test_rejected_is_final_and_does_not_block(trader):
    trader._client.resp = Resp(400, text="invalid stop")
    r = trader.submit(req())
    assert r["status"] == "REJECTED"
    trader._client.resp = Resp(201, {"Location": ".../orders/556"})
    assert trader.submit(req(ticket_id="ticket-0002"))["ok"]


def _tree(oid, instruction="BUY", qty=170, price=338.94, stop=336.0, target=344.82,
          entered="2099-01-01T14:00:00+0000", exit_qty=None):
    leg = {"instruction": instruction, "quantity": qty, "instrument": {"symbol": "BRVO"}}
    exit_instr = "SELL" if instruction == "BUY" else "BUY_TO_COVER"
    xq = qty if exit_qty is None else exit_qty
    xleg = [{"instruction": exit_instr, "quantity": xq, "instrument": {"symbol": "BRVO"}}]
    return {"orderId": oid, "orderStrategyType": "TRIGGER", "quantity": qty, "price": price,
            "enteredTime": entered, "status": "WORKING", "orderLegCollection": [leg],
            "childOrderStrategies": [{"orderStrategyType": "OCO", "childOrderStrategies": [
                {"orderType": "LIMIT", "price": target, "quantity": xq, "orderLegCollection": xleg},
                {"orderType": "STOP", "stopPrice": stop, "quantity": xq, "orderLegCollection": xleg}]}]}


def test_rejected_newer_ticket_does_not_hide_open_position(trader):
    trader._client.resp = Resp(201, {"Location": ".../orders/555"})
    trader.submit(req())                                            # filled earlier, stop gone
    trader._client.resp = Resp(400, text="nope")
    trader.submit(req(ticket_id="ticket-0002"))                     # newer, rejected
    filled = {**_tree(555), "filledQuantity": 170, "status": "FILLED"}
    rows = trader.brackets([filled], pend=[], positions={"BRVO": 170.0}, orders_fresh=True)
    by_id = {r["ticket_id"]: r for r in rows}
    assert by_id["ticket-0001"]["protection"] == "NO_STOP"
    assert by_id["ticket-0002"]["protection"] == "REJECTED"
    stale = trader.brackets([filled], pend=[], positions={}, orders_fresh=False)
    assert {r["ticket_id"]: r for r in stale}["ticket-0001"]["protection"] == "UNKNOWN"


class JsonResp(Resp):
    def __init__(self, status, body):
        super().__init__(status)
        self._body = body

    def json(self):
        return self._body


def _entry(status="WORKING", filled=0, effect="OPENING", instr="SELL_SHORT", strategy="TRIGGER"):
    return {"orderId": 9, "orderStrategyType": strategy, "status": status, "filledQuantity": filled,
            "orderLegCollection": [{"instruction": instr, "positionEffect": effect,
                                    "instrument": {"symbol": "MXR"}}]}


def test_unfilled_entry_rules():
    assert trading.unfilled_entry(_entry())[0]
    assert not trading.unfilled_entry(_entry(filled=10))[0]                       # partly filled
    assert not trading.unfilled_entry(_entry(status="FILLED"))[0]
    assert not trading.unfilled_entry(_entry(effect="CLOSING", instr="BUY_TO_COVER"))[0]   # an exit
    assert not trading.unfilled_entry(_entry(strategy="OCO"))[0]
    assert not trading.unfilled_entry(_entry(effect=None, instr="BUY"))[0]        # cannot tell


class CancelClient(FakeClient):
    def __init__(self, details, cancel_status=200):
        super().__init__()
        self.details, self.cancel_status, self.cancelled = list(details), cancel_status, []

    def order_details(self, _hash, oid):
        return JsonResp(200, self.details.pop(0))

    def cancel_order(self, _hash, oid):
        self.cancelled.append(oid)
        return Resp(self.cancel_status, text="cannot cancel")


def test_cancel_unfilled_entry(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = CancelClient([_entry(), {**_entry(), "status": "CANCELED"}])
    r = trader.cancel_entry(9)
    assert r["ok"] and r["status"] == "CANCELED" and trader._client.cancelled == [9]


def test_cancel_refuses_filled_or_exit_orders(trader):
    trader._client = CancelClient([_entry(filled=35)])
    r = trader.cancel_entry(9)
    assert not r["ok"] and "filled" in r["error"] and trader._client.cancelled == []
    trader._client = CancelClient([_entry(effect="CLOSING", instr="BUY_TO_COVER")])
    assert not trader.cancel_entry(9)["ok"] and trader._client.cancelled == []


def test_cancel_reports_fill_that_beat_the_cancel(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = CancelClient([_entry(), {**_entry(), "status": "FILLED", "filledQuantity": 70}])
    r = trader.cancel_entry(9)
    assert not r["ok"] and "filled before the cancel" in r["error"]
    trader._client = CancelClient([_entry()], cancel_status=400)
    assert "refused the cancel" in trader.cancel_entry(9)["error"]


def test_missing_fill_count_or_live_children_refuse():
    e = _entry()
    del e["filledQuantity"]
    assert not trading.unfilled_entry(e)[0]
    live_kid = {**_entry(), "childOrderStrategies": [{"orderStrategyType": "OCO", "status": "WORKING",
                "childOrderStrategies": [{"orderType": "STOP", "status": "WORKING"}]}]}
    assert not trading.unfilled_entry(live_kid)[0]
    waiting = {**_entry(), "childOrderStrategies": [{"orderStrategyType": "OCO", "status": "WORKING",
               "childOrderStrategies": [{"orderType": "STOP", "status": "AWAITING_PARENT_ORDER"},
                                        {"orderType": "LIMIT", "status": "AWAITING_PARENT_ORDER"}]}]}
    assert trading.unfilled_entry(waiting)[0]      # a normal unfilled bracket stays cancellable


def test_pending_cancel_is_not_reported_as_done(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = CancelClient([_entry()] + [{**_entry(), "status": "PENDING_CANCEL"}] * 10)
    r = trader.cancel_entry(9)
    assert not r["ok"] and "PENDING_CANCEL" in r["error"]


def test_fill_racing_the_cancel_reports_stop_state(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    raced = {**_entry(), "status": "CANCELED", "filledQuantity": 20, "childOrderStrategies": [
        {"orderStrategyType": "OCO", "childOrderStrategies": [{"orderType": "STOP", "status": "CANCELED"}]}]}
    trader._client = CancelClient([_entry(), raced])
    r = trader.cancel_entry(9)
    assert not r["ok"] and "NO LIVE STOP" in r["error"]


def test_cancel_all_only_touches_unfilled_entries(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = CancelClient([_entry(), {**_entry(), "status": "CANCELED"}])
    orders = [_entry(), {**_entry(effect="CLOSING", instr="BUY_TO_COVER"), "orderId": 10},
              {**_entry(filled=5), "orderId": 11}]
    r = trader.cancel_all_entries(orders)
    assert r["count"] == 1 and r["ok"] and trader._client.cancelled == [9]


class Broker(FakeClient):
    """Fake Schwab with order states and one position."""

    def __init__(self, orders, position=("MXR", -57.0, "EQUITY"), stuck=(), auto_cancel=True, after_qty=None,
                 fill_limits=False):
        super().__init__()
        self.orders = {o["orderId"]: o for o in orders}
        self.sym, self.qty, self.asset = position
        self.stuck, self.cancelled = set(stuck), []
        self.auto_cancel, self.after_qty, self.fill_limits = auto_cancel, after_qty, fill_limits
        self._next_fill = 900

    def _fill(self, order, price):
        """Fill an exit order completely and move the position."""
        leg = order["orderLegCollection"][0]
        n = float(leg["quantity"])
        self.qty += n if leg["instruction"].startswith("BUY") else -n
        if self.after_qty is not None:
            self.qty = self.after_qty
        self._next_fill += 1
        oid = self._next_fill
        self.orders[oid] = {"orderId": oid, "status": "FILLED", "filledQuantity": n,
                            "orderActivityCollection": [{"executionType": "FILL", "executionLegs": [
                                {"quantity": n, "price": price}]}]}
        return Resp(201, {"Location": f".../orders/{oid}"})

    def _find(self, oid):
        def walk(o):
            if o.get("orderId") == oid:
                return o
            for ch in o.get("childOrderStrategies") or []:
                hit = walk(ch)
                if hit:
                    return hit
        for o in self.orders.values():
            hit = walk(o)
            if hit:
                return hit

    def order_details(self, _h, oid):
        return JsonResp(200, self._find(oid))

    def cancel_order(self, _h, oid):
        self.cancelled.append(oid)
        if oid not in self.stuck:
            self._find(oid)["status"] = "CANCELED"
        else:
            self._find(oid)["status"] = "PENDING_CANCEL"
        return Resp(200)

    def account_orders(self, _h, start, end, maxResults=None, status=None):
        rows = [o for o in self.orders.values() if status is None or o.get("status") == status]
        return JsonResp(200, rows)

    def replace_order(self, _h, oid, body):
        """Like Schwab/thinkorswim: old stop REPLACED, new stop added to the same OCO."""
        self.replaced = getattr(self, "replaced", []) + [(oid, body)]
        for o in self.orders.values():
            for parent in [o] + list(o.get("childOrderStrategies") or []):
                kids = parent.get("childOrderStrategies") or []
                for k in list(kids):
                    if k.get("orderId") == oid:
                        k["status"] = "REPLACED"
                        new = {**k, "orderId": oid + 1000, "status": "WORKING",
                               "stopPrice": float(body["stopPrice"])}
                        kids.append(new)
                        return Resp(201, {"Location": f".../orders/{oid + 1000}"})
        return Resp(400, text="not found")

    def account_details(self, _h, fields=None):
        pos = [] if not self.qty else [{"instrument": {"symbol": self.sym, "assetType": self.asset},
                                        "longQuantity": max(self.qty, 0), "shortQuantity": max(-self.qty, 0)}]
        return JsonResp(200, {"securitiesAccount": {"positions": pos}})

    def _kill_symbol_orders(self):
        """Real Schwab: entering an order cancels the instrument's other working orders."""
        def kill(o):
            syms = {(l.get("instrument") or {}).get("symbol") for l in o.get("orderLegCollection") or []}
            if o.get("status") in trading.LIVE and (not syms or syms == {self.sym}):
                o["status"] = "CANCELED"
            for ch in o.get("childOrderStrategies") or []:
                kill(ch)
        for o in self.orders.values():
            kill(o)

    def place_order(self, _h, order):
        import copy
        self.placed.append(order)
        if self.auto_cancel:
            self._kill_symbol_orders()
        if order.get("orderType") == "MARKET":
            return self._fill(order, 906.5)
        if order.get("orderType") == "LIMIT" and order.get("orderStrategyType") == "SINGLE" and self.fill_limits:
            return self._fill(order, float(order["price"]))
        # A resting order (limit, new stop, or OCO): give every node an id and WORKING status.
        new = copy.deepcopy(order)
        ids = iter(range(950 + len(self.placed) * 10, 999 + len(self.placed) * 10))

        def stamp(o):
            o["orderId"] = next(ids)
            o["status"] = "WORKING"
            o.setdefault("filledQuantity", 0)
            for k in ("stopPrice", "price"):
                if k in o:
                    o[k] = float(o[k])
            for leg in o.get("orderLegCollection") or []:
                leg.setdefault("positionEffect", "CLOSING")
            if o.get("orderLegCollection"):
                o["quantity"] = o["orderLegCollection"][0]["quantity"]
            for ch in o.get("childOrderStrategies") or []:
                stamp(ch)
        stamp(new)
        self.orders[new["orderId"]] = new
        return Resp(201, {"Location": f".../orders/{new['orderId']}"})


def _leaf(oid, otype, qty, **px):
    return {"orderId": oid, "orderStrategyType": "SINGLE", "status": "WORKING", "orderType": otype,
            "quantity": qty, "filledQuantity": 0, **px,
            "orderLegCollection": [{"instruction": "BUY_TO_COVER", "positionEffect": "CLOSING",
                                    "quantity": qty, "instrument": {"symbol": "MXR", "assetType": "EQUITY"}}]}


def _mu_oco():
    return {"orderId": 50, "orderStrategyType": "OCO", "status": "WORKING", "childOrderStrategies": [
        _leaf(51, "STOP", 57, stopPrice=913.23), _leaf(52, "LIMIT", 57, price=900.18)]}


def test_cancel_symbol_orders_cancels_each_live_exit(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    other = {**_leaf(60, "LIMIT", 10, price=1.0), "orderLegCollection": [
        {"instruction": "SELL", "positionEffect": "CLOSING", "quantity": 10, "instrument": {"symbol": "AKMX"}}]}
    trader._client = Broker([_mu_oco(), other])
    r = trader.cancel_symbol_orders("MXR", list(trader._client.orders.values()), set())
    assert r["ok"] and sorted(trader._client.cancelled) == [51, 52]       # AKMX untouched


def test_close_is_one_market_order_and_schwab_cancels_the_oco(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_mu_oco()])
    r = trader.close_position("MXR", list(trader._client.orders.values()), set())
    assert r["ok"] and r["filled"] == 57 and r["avg"] == 906.5
    (order,) = trader._client.placed
    leg = order["orderLegCollection"][0]
    assert order["orderType"] == "MARKET" and leg["instruction"] == "BUY_TO_COVER" and leg["quantity"] == 57
    assert trader._client.cancelled == []            # no pre-cancel round trip: Schwab did it


def test_close_cleans_up_orders_schwab_left_working(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    entry = {"orderId": 70, "orderStrategyType": "TRIGGER", "status": "FILLED", "filledQuantity": 57,
             "orderLegCollection": [{"instruction": "SELL_SHORT", "positionEffect": "OPENING",
                                     "quantity": 57, "instrument": {"symbol": "MXR"}}],
             "childOrderStrategies": [_mu_oco()]}
    trader._client = Broker([entry], auto_cancel=False)
    r = trader.close_position("MXR", [], set())             # stale snapshot: nothing passed in
    assert r["ok"] and sorted(trader._client.cancelled) == [51, 52] and "left working" in r["message"]


def test_close_flags_a_flipped_position(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_mu_oco()], after_qty=57.0)   # a stop also filled: now long 57
    r = trader.close_position("MXR", [], set())
    assert not r["ok"] and "you are now LONG 57" in r["error"]


def test_spread_orders_are_not_cancelled_by_one_leg(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    spread = {"orderId": 80, "orderStrategyType": "SINGLE", "status": "WORKING", "filledQuantity": 0,
              "orderLegCollection": [
                  {"instruction": "BUY_TO_OPEN", "instrument": {"symbol": "SPY   261120C00600000"}},
                  {"instruction": "SELL_TO_OPEN", "instrument": {"symbol": "SPY   261120C00610000"}}]}
    trader._client = Broker([spread], position=("SPY   261120C00600000", 1.0, "OPTION"), auto_cancel=False)
    r = trader.cancel_symbol_orders("SPY   261120C00600000", [], set())
    assert not r["ok"] and r["mixed"] == [80] and trader._client.cancelled == []


def test_unclear_close_blocks_a_second_close(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_mu_oco()])
    trader._client.place_order = lambda h, o: (_ for _ in ()).throw(TimeoutError("read timed out"))
    r = trader.close_position("MXR", [], set())
    assert r["unresolved_close"] and "MAY have been sent" in r["error"]
    r = trader.close_position("MXR", [], set())
    assert not r["ok"] and "Resolve" in r["error"]
    trader.resolve_close("MXR")
    assert "Resolve" not in (trader.close_position("MXR", [], set()).get("error") or "")


def test_move_stop_keeps_the_oco_and_its_target(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_mu_oco()])                      # MXR short 57, stop 913.23
    r = trader.move_stop("MXR", "910.00", [], set())
    assert r["ok"] and r["oco_kept"] and r["stop"] == "910.00", r
    (oid, body), = trader._client.replaced
    assert oid == 51 and body["stopPrice"] == "910.00"
    assert body["orderLegCollection"][0]["quantity"] == 57


def test_move_standalone_stop_without_target_is_fine(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    lone = {"orderId": 60, "orderStrategyType": "OCO", "status": "WORKING",   # container only to reuse fake
            "childOrderStrategies": [_leaf(61, "STOP", 57, stopPrice=913.23)]}
    trader._client = Broker([lone])
    r = trader.move_stop("MXR", "911.00", [], set())
    assert r["moved"] == 1 and "Stop moved to 911.00" in r["message"]


def test_move_stop_creates_a_stop_when_there_is_none(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([])                               # MXR short 57, no orders at all
    r = trader.move_stop("MXR", "912.00", [], set())
    assert r["ok"] and r["created"], r
    (order,) = trader._client.placed
    assert order["orderStrategyType"] == "SINGLE" and order["orderType"] == "STOP"
    assert order["stopPrice"] == "912.00" and order["orderLegCollection"][0]["instruction"] == "BUY_TO_COVER"
    assert order["orderLegCollection"][0]["quantity"] == 57


def test_new_stop_is_sent_with_the_existing_target_as_one_oco(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_leaf(52, "LIMIT", 57, price=900.18)])   # target only, no stop
    r = trader.move_stop("MXR", "912.00", [], set())
    assert r["ok"] and "one OCO" in r["message"], r
    (order,) = trader._client.placed
    target, stop = order["childOrderStrategies"]
    assert order["orderStrategyType"] == "OCO"
    assert target["orderType"] == "LIMIT" and target["price"] == "900.18"
    assert stop["orderType"] == "STOP" and stop["stopPrice"] == "912.00"


def test_partial_market_then_rebuilds_oco_on_the_rest(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_mu_oco()])                      # short 57, stop 913.23, target 900.18
    r = trader.take_partial("MXR", 0.5, None, [], set(), mode="market")
    assert r["ok"], r
    partial, rebuild = trader._client.placed
    assert partial["orderType"] == "MARKET" and partial["orderLegCollection"][0]["quantity"] == 28
    assert rebuild["orderStrategyType"] == "OCO"
    target, stop = rebuild["childOrderStrategies"]
    assert target["price"] == "900.18" and stop["stopPrice"] == "913.23"
    assert target["orderLegCollection"][0]["quantity"] == 29 and stop["orderLegCollection"][0]["quantity"] == 29


def test_partial_mid_sends_a_limit_at_the_midpoint(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_mu_oco()], fill_limits=True)   # fixture quote 338.70 / 338.77
    r = trader.take_partial("MXR", 0.5, None, [], set(), mode="mid")
    assert r["ok"] and "at the mid" in r["message"], r
    partial = trader._client.placed[0]
    assert partial["orderType"] == "LIMIT" and partial["price"] == "338.74"   # buy to cover: rounded up


def test_partial_mid_not_filled_goes_to_market(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_mu_oco()], fill_limits=False)  # the mid limit just rests
    r = trader.take_partial("MXR", 0.5, None, [], set(), mode="mid")
    kinds = [o["orderType"] if o.get("orderType") else o["orderStrategyType"] for o in trader._client.placed]
    assert kinds == ["LIMIT", "MARKET", "OCO"], kinds
    assert trader._client.cancelled and "rest at market" in r["message"]


def test_partial_with_two_targets_keeps_the_runner_target():
    from companion.orders import pending_orders
    pend = pending_orders([
        {"orderId": 1, "orderStrategyType": "OCO", "status": "WORKING", "childOrderStrategies": [
            _leaf(2, "LIMIT", 28, price=905.0), _leaf(3, "STOP", 28, stopPrice=913.23)]},
        {"orderId": 4, "orderStrategyType": "OCO", "status": "WORKING", "childOrderStrategies": [
            _leaf(5, "LIMIT", 29, price=900.18), _leaf(6, "STOP", 29, stopPrice=913.23)]},
    ])
    stop, target = trading.Trader._exit_plan(pend, "MXR", "SHORT", 28)   # take the first target's shares
    assert stop == 913.23 and target == 900.18


def test_move_stop_refuses_a_price_through_the_market(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_mu_oco()])                      # short: stop must stay above the ask
    r = trader.move_stop("MXR", "300.00", [], set())           # fixture ask is 338.77
    assert not r["ok"] and "would trigger now" in r["error"]
    assert not getattr(trader._client, "replaced", [])


def test_close_when_already_flat_sends_nothing(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([], position=("MXR", 0.0, "EQUITY"))
    r = trader.close_position("MXR", [], set())
    assert r["ok"] and "flat" in r["message"] and trader._client.placed == []


def test_r_basis_uses_entry_fill_and_original_stop(trader):
    trader.submit(req())                                         # BRVO long 170, stop 336.00
    filled = {**_tree(555), "filledQuantity": 170, "status": "FILLED", "orderActivityCollection": [
        {"executionType": "FILL", "executionLegs": [{"quantity": 100, "price": 338.90},
                                                    {"quantity": 70, "price": 338.80}]}]}
    b = trader.r_basis([filled])["BRVO"]
    assert abs(b["entry"] - 338.8588) < 1e-3 and b["stop"] == 336.0
    assert abs(b["rps"] - 2.8588) < 1e-3 and b["side"] == "LONG"


def test_r_basis_follows_an_entry_replaced_in_thinkorswim(trader):
    trader.submit(req())                                         # ticket order 555, stop 336.00
    old = {**_tree(555), "status": "REPLACED", "closeTime": "2099-01-01T14:00:19+0000"}
    new = {**_tree(777, price=339.10, entered="2099-01-01T14:00:19+0000"), "status": "FILLED",
           "orderActivityCollection": [{"executionType": "FILL", "executionLegs": [
               {"quantity": 170, "price": 339.00, "time": "2099-01-01T14:00:20+0000"}]}]}
    b = trader.r_basis([old, new])["BRVO"]
    assert b["entry"] == 339.0 and b["stop"] == 336.0 and b["first_fill"] == "2099-01-01T14:00:20+0000"
    other = {**new, "orderId": 778}                               # two candidates: no guess
    assert "BRVO" not in trader.r_basis([old, new, other])


def test_accepted_but_not_recorded_is_unknown(trader, monkeypatch):
    monkeypatch.setattr(trader, "_safe_update", lambda *a, **k: False)
    r = trader.submit(req())
    assert r["status"] == "UNKNOWN" and not r["ok"]
    assert trader.store.get_ticket("ticket-0001")["status"] == "SENDING"   # still blocks


def test_reconcile_requires_full_unique_match(trader, monkeypatch):
    trader._client.exc = TimeoutError()
    trader.submit(req())
    t = trader.store.get_ticket("ticket-0001")
    now = t["created"][:19].replace("T", "T") + "+0000"
    wrong_side = _tree(1, instruction="SELL_SHORT", entered=now)
    trader.reconcile([wrong_side])
    assert trader.store.get_ticket("ticket-0001")["status"] == "UNKNOWN"
    trader.reconcile([_tree(2, entered=now), _tree(3, entered=now)])       # ambiguous
    assert trader.store.get_ticket("ticket-0001")["status"] == "UNKNOWN"
    trader.reconcile([_tree(5, entered=now, exit_qty=100)])               # exits do not match
    assert trader.store.get_ticket("ticket-0001")["status"] == "UNKNOWN"
    trader.reconcile([_tree(4, entered=now)])
    t = trader.store.get_ticket("ticket-0001")
    assert t["status"] == "ACCEPTED" and t["order_id"] == 4


def _mu_quote(trader, monkeypatch, bid=906.0, ask=906.2):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    monkeypatch.setattr(trader, "quote", lambda s, max_age=0: {"bid": bid, "ask": ask})


def test_set_target_in_r_sends_one_oco_with_the_current_stop(trader, monkeypatch):
    _mu_quote(trader, monkeypatch)
    trader._client = Broker([_mu_oco()])                      # short 57, stop 913.23, target 900.18
    basis = {"side": "SHORT", "entry": 910.0, "rps": 3.0}     # frozen R from the ticket
    r = trader.set_target("MXR", None, 3, [], set(), basis)
    assert r["ok"] and r["target"] == "901.00", r
    (order,) = trader._client.placed
    target, stop = order["childOrderStrategies"]
    assert order["orderStrategyType"] == "OCO" and target["price"] == "901.00"
    assert stop["stopPrice"] == "913.23" and stop["orderLegCollection"][0]["quantity"] == 57


def test_set_target_at_a_price_and_refusals(trader, monkeypatch):
    _mu_quote(trader, monkeypatch)
    trader._client = Broker([_mu_oco()])
    assert not trader.set_target("MXR", None, 2, [], set(), None)["ok"]           # no R basis
    assert "through the market" in trader.set_target("MXR", "907", None, [], set(), None)["error"]
    assert trader._client.placed == []
    r = trader.set_target("MXR", "895.5", None, [], set(), None)
    assert r["ok"] and trader._client.placed[0]["childOrderStrategies"][0]["price"] == "895.50"


def test_set_target_without_a_stop_sends_a_lone_target(trader, monkeypatch):
    _mu_quote(trader, monkeypatch)
    trader._client = Broker([])
    r = trader.set_target("MXR", "900", None, [], set(), None)
    (order,) = trader._client.placed
    assert r["ok"] and "no stop" in r["message"]
    assert order["orderType"] == "LIMIT" and order["orderLegCollection"][0]["instruction"] == "BUY_TO_COVER"


def _quote_mark(trader, monkeypatch, mark=906.0):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    monkeypatch.setattr(trader, "quote", lambda s, max_age=0: {"bid": mark - 0.1, "ask": mark + 0.1, "mark": mark})


def test_trail_replaces_exits_with_target_and_trailing_stop(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_oco()])                      # short 57, stop 913.23, target 900.18
    r = trader.set_trail("MXR", "$", 2.5, "MARK", [], set(), None)
    assert r["ok"] and not r["looser"], r                     # 906 + 2.50 = 908.50 tightens 913.23
    (order,) = trader._client.placed
    target, stop = order["childOrderStrategies"]
    assert target["price"] == "900.18"
    assert stop["orderType"] == "TRAILING_STOP" and stop["stopPriceOffset"] == 2.5
    assert stop["orderLegCollection"][0]["instruction"] == "BUY_TO_COVER"


def test_trail_looser_than_stop_is_sent_with_a_note(trader, monkeypatch):
    _quote_mark(trader, monkeypatch, mark=912.0)
    trader._client = Broker([_mu_oco()])
    r = trader.set_trail("MXR", "$", 3, "MARK", [], set(), None)   # starts 915.00, looser than 913.23
    assert r["ok"] and r["looser"] and "looser than your stop" in r["message"]


def test_trail_in_r_needs_the_frozen_basis(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_oco()])
    assert "frozen 1R" in trader.set_trail("MXR", "R", 1, "MARK", [], set(), None)["error"]
    r = trader.set_trail("MXR", "R", 1, "MARK", [], set(), {"side": "SHORT", "rps": 3.54})
    assert r["ok"] and trader._client.placed[0]["childOrderStrategies"][1]["stopPriceOffset"] == 3.54


def _mu_trailing():
    trail = {**_leaf(51, "TRAILING_STOP", 57), "stopPriceOffset": 2.5, "stopPriceLinkType": "VALUE",
             "stopPriceLinkBasis": "MARK"}
    return {"orderId": 50, "orderStrategyType": "OCO", "status": "WORKING",
            "childOrderStrategies": [trail, _leaf(52, "LIMIT", 57, price=900.18)]}


def test_partial_on_a_trailing_position_rebuilds_a_trailing_stop(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_trailing()])
    r = trader.take_partial("MXR", 0.5, None, [], set(), mode="market")
    assert r["ok"], r
    rebuild = trader._client.placed[-1]
    target, stop = rebuild["childOrderStrategies"]
    assert stop["orderType"] == "TRAILING_STOP" and stop["stopPriceOffset"] == 2.5
    assert stop["orderLegCollection"][0]["quantity"] == 29 and target["price"] == "900.18"


def test_move_stop_on_a_trailing_position_goes_back_to_fixed(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_trailing()])
    r = trader.move_stop("MXR", "910.00", [], set())
    assert r["ok"], r
    (order,) = trader._client.placed
    target, stop = order["childOrderStrategies"]
    assert stop["orderType"] == "STOP" and stop["stopPrice"] == "910.00" and target["price"] == "900.18"


def test_set_target_keeps_the_trailing_stop(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_trailing()])
    r = trader.set_target("MXR", "898.00", None, [], set(), None)
    assert r["ok"], r
    target, stop = trader._client.placed[0]["childOrderStrategies"]
    assert target["price"] == "898.00" and stop["orderType"] == "TRAILING_STOP"


def test_autobe_ticket_sends_a_fixed_stop_and_records_one_move(trader):
    r = trader.submit(req(stop_type="autobe"))
    assert r["ok"], r
    stop = trader._client.placed[0]["childOrderStrategies"][0]["childOrderStrategies"][1]
    assert stop["orderType"] == "STOP" and stop["stopPrice"] == "336.00"
    assert r["plan"]["be_trigger"] == "341.88"                       # 338.94 + 1R (2.94)
    assert trader.store.auto_be("ticket-0001")["state"] == "PENDING"
    trader.submit(req(ticket_id="ticket-0002"))
    assert trader.store.auto_be("ticket-0002") is None              # a fixed ticket never moves


def _ticket_tree(oco, oid=40):
    """The ticket's filled entry order with its exits underneath, as Schwab returns it."""
    return {"orderId": oid, "orderStrategyType": "TRIGGER", "status": "FILLED", "filledQuantity": 57,
            "orderLegCollection": [{"instruction": "SELL_SHORT", "positionEffect": "OPENING", "quantity": 57,
                                    "instrument": {"symbol": "MXR", "assetType": "EQUITY"}}],
            "childOrderStrategies": [oco]}


def _claimed(trader, tid="t-mxr"):
    trader.store.insert_ticket({"ticket_id": tid, "symbol": "MXR", "side": "SHORT", "qty": 57, "limit": "908.00",
                                "stop": "913.23", "target": "900.18", "risk": 300.0, "request": {}})
    trader.store.update_ticket(tid, "ACCEPTED", order_id=40)
    trader.store.add_auto_be(tid, "MXR")
    assert trader.store.set_auto_be(tid, "SENDING", only_from="PENDING")
    assert not trader.store.set_auto_be(tid, "SENDING", only_from="PENDING")   # claimed once only
    return tid


def test_auto_breakeven_moves_the_stop_once_never_below_entry(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_ticket_tree(_mu_oco())])              # MXR short 57, stop 913.23
    r = trader.auto_breakeven(_claimed(trader), "MXR", "SHORT", 908.004, [], set())
    assert r["ok"] and r["state"] == "DONE", r
    (oid, body), = trader._client.replaced
    assert oid == 51 and body["stopPrice"] == "908.00"        # short rounds down: never a locked-in loss
    assert trader.store.auto_be("t-mxr")["state"] == "DONE"
    assert any(a["kind"] == "auto_be" for a in trader.store.actions_since("2000-01-01"))


def test_auto_breakeven_never_loosens_or_replaces_a_trail(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_ticket_tree(_mu_oco())])              # stop 913.23 already past B/E 914
    r = trader.auto_breakeven(_claimed(trader), "MXR", "SHORT", 914.0, [], set())
    assert r["state"] == "DONE" and not getattr(trader._client, "replaced", [])
    trader._client = Broker([_ticket_tree(_mu_trailing())])
    r = trader.auto_breakeven(_claimed(trader, "t2"), "MXR", "SHORT", 908.0, [], set())
    assert r["state"] == "SKIPPED" and "trailing" in r["message"] and trader._client.placed == []
    trader._client = Broker([_mu_oco()], position=("MXR", 0.0, "EQUITY"))
    r = trader.auto_breakeven(_claimed(trader, "t3"), "MXR", "SHORT", 908.0, [], set())
    assert r["state"] == "SKIPPED" and not getattr(trader._client, "replaced", [])


def test_auto_move_checks_the_final_read_of_each_stop(trader, monkeypatch):
    # The stop is tightened in thinkorswim between the snapshot and the final read: left alone.
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_mu_oco()])
    real = trader._client.order_details

    def tightened(h, oid):
        if oid == 51:
            return JsonResp(200, {**trader._client._find(51), "stopPrice": 905.0})
        return real(h, oid)
    trader._client.order_details = tightened
    r = trader._move_stop_locked("MXR", Decimal("908.00"), [], set(), only="SHORT", allowed={50, 51, 52})
    assert not r["ok"] and "not loosened" in r["error"] and not getattr(trader._client, "replaced", [])
    # The automatic move never creates a stop or rebuilds a trail, and checks the side.
    trader._client = Broker([])
    r = trader._move_stop_locked("MXR", Decimal("908.00"), [], set(), only="SHORT")
    assert not r["ok"] and trader._client.placed == []
    trader._client = Broker([_mu_trailing()])
    r = trader._move_stop_locked("MXR", Decimal("908.00"), [], set(), only="SHORT")
    assert not r["ok"] and trader._client.placed == []
    trader._client = Broker([_mu_oco()])
    r = trader._move_stop_locked("MXR", Decimal("908.00"), [], set(), only="LONG")
    assert not r["ok"] and "SHORT now" in r["error"]


def test_auto_breakeven_leaves_a_stop_from_another_order_alone(trader, monkeypatch):
    # Closed and re-entered in thinkorswim: the working stop belongs to the new order, not the ticket.
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    old = _ticket_tree({"orderId": 45, "orderStrategyType": "OCO", "status": "CANCELED", "childOrderStrategies": [
        {**_leaf(46, "STOP", 57, stopPrice=913.23), "status": "CANCELED"}]})
    trader._client = Broker([old, _mu_oco()])
    r = trader.auto_breakeven(_claimed(trader), "MXR", "SHORT", 908.0, [], set())
    assert r["state"] == "SKIPPED" and "not from this ticket" in r["message"]
    assert not getattr(trader._client, "replaced", [])
    r = trader._move_stop_locked("MXR", Decimal("908.00"), [], set(), only="SHORT", allowed={40, 45, 46})
    assert not r["ok"] and not getattr(trader._client, "replaced", [])


def test_auto_breakeven_waits_when_the_market_is_closed(trader, monkeypatch):
    monkeypatch.setattr(trading, "market_open", lambda now=None: False)
    trader._client = Broker([_mu_oco()])
    r = trader.auto_breakeven(_claimed(trader), "MXR", "SHORT", 908.0, [], set())
    assert r["state"] == "PENDING" and trader.store.auto_be("t-mxr")["state"] == "PENDING"


def test_trail_verification_ignores_an_old_trailing_order(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_trailing()], auto_cancel=False)     # the old $2.50 trail still shows
    real_place = trader._client.place_order
    trader._client.place_order = lambda h, o: (real_place(h, o), Resp(201, {"Location": ".../orders/77777"}))[1]
    r = trader.set_trail("MXR", "$", 1.0, "MARK", [], set(), None)
    assert not r["ok"] and "Not confirmed" in r["message"]            # never confirmed by the old order


def test_partial_with_server_error_is_unknown_and_blocks_the_next_action(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_oco()])
    trader._client.place_order = lambda h, o: Resp(503, text="upstream")
    r = trader.take_partial("MXR", 0.5, None, [], set(), mode="market")
    assert not r["ok"] and r["unresolved_close"] and "MAY have been sent" in r["error"]
    again = trader.take_partial("MXR", 0.5, None, [], set(), mode="market")
    assert again["unresolved_close"] and "Resolve" in again["error"]
    assert trader.move_stop("MXR", "910.00", [], set())["unresolved_close"]       # stops too
    trader.resolve_close("MXR")
    assert not trader.move_stop("MXR", "910.00", [], set()).get("unresolved_close")


def test_a_close_still_working_blocks_until_schwab_says_it_is_final(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_oco()])
    trader.store.log_action("close", "MXR", 4242, "WORKING", "BUY_TO_COVER 0/57")
    trader._client.orders[4242] = {"orderId": 4242, "status": "WORKING"}
    assert trader.close_position("MXR", [], set())["unresolved_close"]
    trader._client.orders[4242]["status"] = "FILLED"
    assert not trader.close_position("MXR", [], set()).get("unresolved_close")    # checked again: final


def test_partial_never_rebuilds_exits_for_a_flipped_position(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_oco()], after_qty=20.0)       # short 57, after the partial: LONG 20
    r = trader.take_partial("MXR", 0.5, None, [], set(), mode="market")
    assert not r["ok"] and "now LONG 20" in r["error"]
    assert len(trader._client.placed) == 1                      # only the partial, no exits sent


def test_partial_protects_only_what_is_surely_left(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_oco()], after_qty=-40.0)      # sold 28 of 57 but Schwab shows 40
    r = trader.take_partial("MXR", 0.5, None, [], set(), mode="market")
    stop = trader._client.placed[-1]["childOrderStrategies"][1]
    assert stop["orderLegCollection"][0]["quantity"] == 29 and "CHECK" in r["message"]


def test_trading_client_uses_the_dashboard_account(trader, monkeypatch):
    class Accts:
        def linked_accounts(self):
            return JsonResp(200, [{"hashValue": "A"}, {"hashValue": "B"}])
    monkeypatch.setattr(trading.schwab, "make_trading_client", lambda: Accts())
    trader._client = None
    trader.expected_hash = "B"
    trader._ensure()
    assert trader._hash == "B"
    trader._client, trader.expected_hash = None, None
    with pytest.raises(RuntimeError):
        trader._ensure()                                        # two accounts, none picked


def test_server_refuses_a_foreign_host_header():
    from fastapi.testclient import TestClient
    from companion.app import app
    c = TestClient(app, base_url="http://evil.example:8770")
    assert c.get("/api/ui/prefs").status_code == 403
    assert TestClient(app, base_url="http://127.0.0.1:8770").get("/api/ui/prefs").status_code == 200


def test_mid_partial_whose_market_rest_errors_stays_unresolved(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_oco()], fill_limits=False)     # the mid limit rests unfilled
    real = trader._client.place_order
    calls = []

    def place(h, o):
        calls.append(o)
        return real(h, o) if len(calls) == 1 else Resp(502, text="bad gateway")   # market rest: 5xx
    trader._client.place_order = place
    r = trader.take_partial("MXR", 0.5, None, [], set(), mode="mid")
    assert r["unresolved_close"]
    assert trader.take_partial("MXR", 0.5, None, [], set(), mode="mid")["unresolved_close"]


def test_new_entry_refused_while_a_close_is_open(trader):
    trader.store.log_action("close", "BRVO", None, "UNKNOWN", "no answer")
    r = trader.submit(req())
    assert not r["ok"] and "Resolve" in r["error"] and trader._client.placed == []


def test_account_switch_blocks_trading(trader):
    trader._hash = "A" * 64
    trader.expected_hash = "B" * 64
    with pytest.raises(RuntimeError):
        trader._ensure()


def test_status_polling_is_not_sent_during_a_429_pause(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_oco()])
    trader.cooldown_left = lambda: 20.0
    asked = []
    real = trader._client.order_details
    trader._client.order_details = lambda h, o: (asked.append(o), real(h, o))[1]
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    r = trader.take_partial("MXR", 0.5, None, [], set(), mode="market")
    assert asked == []                                   # no detail polling during the pause
    stop = trader._client.placed[-1]["childOrderStrategies"][1]
    assert stop["orderLegCollection"][0]["quantity"] == 29 and r["unresolved_close"]


def test_rebuild_during_a_pause_waits_at_most_once(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_oco()])
    trader.cooldown_left = lambda: 30.0
    slept = []
    monkeypatch.setattr(trading.time, "sleep", lambda s: slept.append(s))
    trader.take_partial("MXR", 0.5, None, [], set(), mode="market")
    assert sum(slept) <= 5.0 * 3 + 1          # a few bounded position/order reads, never a 30 s hold
    assert trader._client.placed[-1]["orderStrategyType"] == "OCO"


def test_partial_at_bid_and_ask_uses_the_literal_price(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    monkeypatch.setattr(trader, "quote", lambda s, max_age=0: {"bid": 906.10, "ask": 906.30, "mark": 906.2})
    trader._client = Broker([_mu_oco()], fill_limits=True)          # short: covers are buys
    trader.take_partial("MXR", 0.5, None, [], set(), mode="bid")
    assert trader._client.placed[0]["price"] == "906.10"             # cover at the bid: rests
    trader._client = Broker([_mu_oco()], fill_limits=True)
    trader.take_partial("MXR", 0.5, None, [], set(), mode="ask")
    assert trader._client.placed[0]["price"] == "906.30"             # cover at the ask: takes
