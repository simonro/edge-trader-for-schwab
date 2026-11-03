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


def test_entry_rejected_after_acceptance_shows_schwabs_reason(trader):
    trader._client.resp = Resp(201, {"Location": ".../orders/555"})
    trader.submit(req())
    refused = {**_tree(555), "status": "REJECTED",
               "statusDescription": "You do not have enough available cash/buying power for this order."}
    (row,) = trader.brackets([refused], pend=[], positions={}, orders_fresh=True)
    assert row["protection"] == "NOT_FILLED" and "buying power" in row["entry_reason"]


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
                 fill_limits=False, ins=None, others=()):
        super().__init__()
        self.ins, self.others = ins or {}, list(others)   # extra instrument fields; other positions held
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
        pos = [] if not self.qty else [{"instrument": {"symbol": self.sym, "assetType": self.asset, **self.ins},
                                        "longQuantity": max(self.qty, 0), "shortQuantity": max(-self.qty, 0)}]
        pos += self.others
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
    assert [st for st, _ in trader.store.stops_since("MXR", "2000-01-01")] == ["910.00"]   # recorded for R


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


def test_auto_breakeven_follows_an_oco_the_companion_rebuilt(trader, monkeypatch):
    # META 2026-11-02: Replace target rebuilt the OCO and Auto B/E then skipped at +1R.
    _mu_quote(trader, monkeypatch)
    trader._client = Broker([_ticket_tree(_mu_oco())])              # MXR short 57, stop 913.23
    trader.store.insert_ticket({"ticket_id": "t-mxr", "symbol": "MXR", "side": "SHORT", "qty": 57, "limit": "908.00",
                                "stop": "913.23", "target": "900.18", "risk": 300.0, "request": {}})
    trader.store.update_ticket("t-mxr", "ACCEPTED", order_id=40)
    trader.store.add_auto_be("t-mxr", "MXR")
    r = trader.set_target("MXR", "898.00", None, [], set(), None)
    assert r["ok"] and "Auto B/E stays on this stop" in r["message"], r
    new_oco = trader._client.placed[0]
    assert trader.store.auto_be_orders("t-mxr") == [max(trader._client.orders)]
    assert new_oco["orderStrategyType"] == "OCO"
    assert trader.store.set_auto_be("t-mxr", "SENDING", only_from="PENDING")
    r = trader.auto_breakeven("t-mxr", "MXR", "SHORT", 908.0, [], set())
    assert r["state"] == "DONE", r
    (oid, body), = trader._client.replaced
    assert body["stopPrice"] == "908.00"
    # A trailing stop the companion sets is never linked: Auto B/E does not touch trails.
    assert trader.store.link_auto_be("MXR", 1, "2000-01-01") == []   # no PENDING ticket left


def test_a_target_that_fills_as_it_enters_reports_the_close(trader, monkeypatch):
    # TSLA 2026-11-02: the new target filled within a second and the result said "check thinkorswim".
    _mu_quote(trader, monkeypatch)

    class FillsAtOnce(Broker):
        def place_order(self, h, order):
            r = super().place_order(h, order)
            oco = self.orders[max(self.orders)]
            tgt, stop = oco["childOrderStrategies"]
            tgt.update(status="FILLED", filledQuantity=57, orderActivityCollection=[
                {"executionType": "FILL", "executionLegs": [{"quantity": 57, "price": 899.5}]}])
            stop["status"], oco["status"], self.qty = "CANCELED", "FILLED", 0.0
            return r
    trader._client = FillsAtOnce([_mu_oco()])
    r = trader.set_target("MXR", "900.00", None, [], set(), None)
    assert r["ok"] and "as it entered: position closed" in r["message"], r
    assert "check thinkorswim" not in r["message"]


def _pending_autobe(trader, tid="t-mxr"):
    trader.store.insert_ticket({"ticket_id": tid, "symbol": "MXR", "side": "SHORT", "qty": 57, "limit": "908.00",
                                "stop": "913.23", "target": "900.18", "risk": 300.0, "request": {}})
    trader.store.update_ticket(tid, "ACCEPTED", order_id=40)
    trader.store.add_auto_be(tid, "MXR")
    return tid


def _filled_entry(oco, when="2026-11-02T14:00:00+0000"):
    t = _ticket_tree(oco)
    t["orderActivityCollection"] = [{"executionType": "FILL", "executionLegs": [{"quantity": 57, "price": 908.0, "time": when}]}]
    return t


def test_auto_breakeven_refuses_a_position_re_entered_by_another_order(trader, monkeypatch):
    # ChatGPT audit 2026-11-02 (high): a close and re-entry in thinkorswim must not inherit the
    # old ticket's consent, even when the companion linked the new exits to it.
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    reentry = {"orderId": 70, "orderStrategyType": "SINGLE", "status": "FILLED", "filledQuantity": 57,
               "orderLegCollection": [{"instruction": "SELL_SHORT", "quantity": 57, "instrument": {"symbol": "MXR"}}],
               "orderActivityCollection": [{"executionType": "FILL", "executionLegs": [
                   {"quantity": 57, "price": 905.0, "time": "2026-11-02T14:30:00+0000"}]}]}
    trader._client = Broker([_filled_entry(_mu_oco()), reentry])
    tid = _pending_autobe(trader)
    trader.store.link_auto_be("MXR", 50, "2000-01-01")
    assert trader.store.set_auto_be(tid, "SENDING", only_from="PENDING")
    r = trader.auto_breakeven(tid, "MXR", "SHORT", 908.0, [], set())
    assert r["state"] == "SKIPPED" and "re-entered by order 70" in r["message"], r
    assert not getattr(trader._client, "replaced", [])
    # An entry fill from BEFORE the ticket's own fill is not a re-entry.
    reentry["orderActivityCollection"][0]["executionLegs"][0]["time"] = "2026-11-02T13:00:00+0000"
    trader._client = Broker([_filled_entry(_mu_oco()), reentry])
    tid2 = _pending_autobe(trader, "t2")
    assert trader.store.set_auto_be(tid2, "SENDING", only_from="PENDING")
    assert trader.auto_breakeven(tid2, "MXR", "SHORT", 908.0, [], set())["state"] == "DONE"


def test_auto_breakeven_rereads_an_old_gtc_entry_that_looks_open(trader, monkeypatch):
    # ChatGPT audit pass 2 (high): an older GTC entry filled after the ticket, but the poller's
    # snapshot still shows it WORKING and today's order read does not include it.
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    leg = [{"instruction": "SELL_SHORT", "quantity": 57, "instrument": {"symbol": "MXR"}}]
    stale = {"orderId": 80, "orderStrategyType": "SINGLE", "status": "WORKING", "filledQuantity": 0,
             "duration": "GOOD_TILL_CANCEL", "orderLegCollection": leg}
    filled = {**stale, "status": "FILLED", "filledQuantity": 57, "orderActivityCollection": [
        {"executionType": "FILL", "executionLegs": [{"quantity": 57, "price": 905.0, "time": "2026-11-02T14:30:00+0000"}]}]}

    class OldGtc(Broker):
        def order_details(self, h, oid):
            return JsonResp(200, filled) if oid == 80 else super().order_details(h, oid)
    trader._client = OldGtc([_filled_entry(_mu_oco())])
    tid = _pending_autobe(trader)
    assert trader.store.set_auto_be(tid, "SENDING", only_from="PENDING")
    r = trader.auto_breakeven(tid, "MXR", "SHORT", 908.0, [stale], set())
    assert r["state"] == "SKIPPED" and "order 80" in r["message"], r
    assert not getattr(trader._client, "replaced", [])
    # Pass 3: the same entry nested inside an older OCO group.
    group = {"orderId": 79, "orderStrategyType": "OCO", "status": "WORKING", "childOrderStrategies": [stale]}
    trader._client = OldGtc([_filled_entry(_mu_oco())])
    tid2 = _pending_autobe(trader, "t2")
    assert trader.store.set_auto_be(tid2, "SENDING", only_from="PENDING")
    r = trader.auto_breakeven(tid2, "MXR", "SHORT", 908.0, [group], set())
    assert r["state"] == "SKIPPED" and "order 80" in r["message"], r


def test_a_claimed_ticket_still_gets_the_rebuilt_exits(trader, monkeypatch):
    # ChatGPT audit 2026-11-02 (high): +1R claimed while Replace target holds the order lock.
    _mu_quote(trader, monkeypatch)
    trader._client = Broker([_filled_entry(_mu_oco())])
    tid = _pending_autobe(trader)
    assert trader.store.set_auto_be(tid, "SENDING", only_from="PENDING")
    assert trader.set_target("MXR", "898.00", None, [], set(), None)["ok"]
    assert trader.auto_breakeven(tid, "MXR", "SHORT", 908.0, [], set())["state"] == "DONE"


def test_the_link_is_kept_when_the_check_read_is_late(trader, monkeypatch):
    # ChatGPT audit 2026-11-02 (medium): Schwab accepted the OCO but the read after it missed it.
    _mu_quote(trader, monkeypatch)
    trader._client = Broker([_filled_entry(_mu_oco())])
    tid = _pending_autobe(trader)
    real, calls = trader._fresh_orders, []

    def late(client, orders=None):
        calls.append(1)
        return real(client, orders) if len(calls) == 1 else []
    monkeypatch.setattr(trader, "_fresh_orders", late)
    r = trader.set_target("MXR", "898.00", None, [], set(), None)
    assert not r["ok"]                                              # not confirmed: says so
    assert trader.store.auto_be_orders(tid) == [max(trader._client.orders)]


def test_a_filled_target_is_only_a_close_when_the_account_is_flat(trader, monkeypatch):
    # ChatGPT audit 2026-11-02 (medium): the exit size can be smaller than the position.
    _mu_quote(trader, monkeypatch)

    class FillsButNotFlat(Broker):
        def place_order(self, h, order):
            r = super().place_order(h, order)
            oco = self.orders[max(self.orders)]
            tgt, stop = oco["childOrderStrategies"]
            tgt.update(status="FILLED", filledQuantity=57)
            stop["status"], oco["status"], self.qty = "CANCELED", "FILLED", -28.0
            return r
    trader._client = FillsButNotFlat([_mu_oco()])
    r = trader.set_target("MXR", "900.00", None, [], set(), None)
    assert not r["ok"] and "28 shares are still open" in r["message"] and "position closed" not in r["message"], r


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


# ---- options: price grids, contracts wording, and trailing stops behind Schwab's preview -------

OPT = "AAPL  261120C00230000"


class OptBroker(Broker):
    """The Broker fake holding a long option position, with Schwab's order preview."""

    def __init__(self, orders, qty=4.0, preview=None, preview_exc=None, **kw):
        super().__init__(orders, position=(OPT, qty, "OPTION"), **kw)
        self.previewed, self.preview_resp, self.preview_exc = [], preview, preview_exc

    def preview_order(self, _h, order):
        self.previewed.append(order)
        if self.preview_exc:
            raise self.preview_exc
        return self.preview_resp or JsonResp(200, {"orderValidationResult": {"rejects": []}})


def _opt_leaf(oid, otype, qty, **px):
    return {"orderId": oid, "orderStrategyType": "SINGLE", "status": "WORKING", "orderType": otype,
            "quantity": qty, "filledQuantity": 0, **px,
            "orderLegCollection": [{"instruction": "SELL_TO_CLOSE", "positionEffect": "CLOSING",
                                    "quantity": qty, "instrument": {"symbol": OPT, "assetType": "OPTION"}}]}


def _opt_oco(stop=0.30, target=0.95, qty=4):
    return {"orderId": 50, "orderStrategyType": "OCO", "status": "WORKING", "childOrderStrategies": [
        _opt_leaf(51, "STOP", qty, stopPrice=stop), _opt_leaf(52, "LIMIT", qty, price=target)]}


def _opt_quote(trader, monkeypatch, bid=0.55, ask=0.60, penny=False, underlying="AAPL"):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    q = {"bid": bid, "ask": ask, "mark": round((bid + ask) / 2, 4), "asset_type": "OPTION",
         "underlying": underlying, "is_penny": penny}
    monkeypatch.setattr(trader, "quote", lambda s, max_age=0: q)


def test_option_move_stop_lands_on_the_nickel_grid(trader, monkeypatch):
    _opt_quote(trader, monkeypatch)
    trader._client = OptBroker([_opt_oco()])
    r = trader.move_stop(OPT, "0.37", [], set())
    assert r["ok"] and r["stop"] == "0.40", r                  # long: the tighter tick, never 0.35
    (oid, body), = trader._client.replaced
    assert oid == 51 and body["stopPrice"] == "0.40"
    assert body["orderLegCollection"][0]["instrument"]["assetType"] == "OPTION"
    assert "4 contracts" in r["message"] and "shares" not in r["message"]


def test_option_move_stop_rounds_a_short_stop_down(trader, monkeypatch):
    # Short 4 calls (bid 0.55 / ask 0.60): a stop typed 0.73 goes to 0.70 on the nickel grid, not 0.75.
    _opt_quote(trader, monkeypatch)
    oco = _opt_oco(stop=0.80, target=0.30)
    for k in oco["childOrderStrategies"]:
        k["orderLegCollection"][0]["instruction"] = "BUY_TO_CLOSE"
    trader._client = OptBroker([oco], qty=-4)
    r = trader.move_stop(OPT, "0.73", [], set())
    assert r["ok"] and r["stop"] == "0.70", r


def test_option_move_stop_on_a_penny_class_keeps_pennies(trader, monkeypatch):
    _opt_quote(trader, monkeypatch, penny=True)
    trader._client = OptBroker([_opt_oco()])
    r = trader.move_stop(OPT, "0.37", [], set())
    assert r["ok"] and trader._client.replaced[0][1]["stopPrice"] == "0.37"


def test_option_set_target_refuses_an_off_grid_price(trader, monkeypatch):
    _opt_quote(trader, monkeypatch)
    trader._client = OptBroker([_opt_oco()])
    r = trader.set_target(OPT, "0.93", None, [], set(), None)     # nickel grid: sent as typed or not at all
    assert not r["ok"] and "0.05 steps" in r["error"] and trader._client.placed == []
    r = trader.set_target(OPT, "0.95", None, [], set(), None)
    assert r["ok"] and r["target"] == "0.95", r
    (order,) = trader._client.placed
    target, stop = order["childOrderStrategies"]
    assert target["price"] == "0.95" and stop["stopPrice"] == "0.30"
    assert target["orderLegCollection"][0]["instruction"] == "SELL_TO_CLOSE"
    assert trader._client.previewed == []                     # a fixed stop needs no preview


def test_option_target_in_r_uses_the_frozen_premium_basis(trader, monkeypatch):
    _opt_quote(trader, monkeypatch)
    trader._client = OptBroker([_opt_oco()])
    r = trader.set_target(OPT, None, 2, [], set(), {"side": "LONG", "entry": 0.52, "rps": 0.22})
    assert r["ok"] and r["target"] == "0.95", r               # 0.52 + 0.44 = 0.96, floored to 0.95


def test_option_partial_at_the_mid_uses_option_ticks(trader, monkeypatch):
    _opt_quote(trader, monkeypatch, bid=0.55, ask=0.70)
    trader._client = OptBroker([_opt_oco()], fill_limits=True)
    r = trader.take_partial(OPT, 0.5, None, [], set(), mode="mid")
    assert r["ok"], r
    partial, rebuild = trader._client.placed
    assert partial["price"] == "0.60"                         # mid 0.625, sell rounds down on 0.05s
    assert partial["orderLegCollection"][0]["instruction"] == "SELL_TO_CLOSE"
    assert rebuild["childOrderStrategies"][1]["stopPrice"] == "0.30"
    assert "contracts" in r["message"] or "Took 2 of 4" in r["message"]
    assert "share" not in trader.take_partial(OPT, 0.1, None, [], set())["error"]


def test_option_trail_is_sent_when_schwab_preview_accepts_it(trader, monkeypatch):
    _opt_quote(trader, monkeypatch, bid=0.70, ask=0.75)
    trader._client = OptBroker([_opt_oco()])
    r = trader.set_trail(OPT, "$", "0.13", "MARK", [], set(), None)
    assert r["ok"], r
    (previewed,) = trader._client.previewed
    (order,) = trader._client.placed
    assert previewed == order
    target, stop = order["childOrderStrategies"]
    assert stop["orderType"] == "TRAILING_STOP" and stop["stopPriceOffset"] == 0.15   # 0.13 -> nickel
    assert stop["orderLegCollection"][0]["instruction"] == "SELL_TO_CLOSE" and target["price"] == "0.95"


def test_option_trail_refused_by_the_preview_sends_nothing(trader, monkeypatch):
    _opt_quote(trader, monkeypatch, bid=0.70, ask=0.75)
    no = JsonResp(200, {"orderValidationResult": {"rejects": [
        {"validationRuleName": "TrailingStop", "message": "Trailing stop orders are not allowed for options."}]}})
    trader._client = OptBroker([_opt_oco()], preview=no)
    r = trader.set_trail(OPT, "$", "0.15", "MARK", [], set(), None)
    assert not r["ok"] and "does not accept" in r["error"] and "not allowed for options" in r["error"]
    assert "still working" in r["error"] and trader._client.placed == []
    assert trader._client.orders[50]["childOrderStrategies"][0]["status"] == "WORKING"   # stop untouched


@pytest.mark.parametrize("kw", [{"preview": Resp(500, text="boom")}, {"preview_exc": TimeoutError("read timed out")},
                                {"preview": Resp(429)}, {"preview": JsonResp(200, {})}, {"preview": JsonResp(200, None)},
                                {"preview": JsonResp(200, [])}, {"preview": JsonResp(200, {"error": "invalid"})},
                                {"preview": JsonResp(200, {"orderValidationResult": {"rejects": "no"}})},
                                {"preview": JsonResp(200, {"orderValidationResult": {"rejects": None}})}])
def test_option_trail_without_a_clear_preview_is_refused(trader, monkeypatch, kw):
    _opt_quote(trader, monkeypatch, bid=0.70, ask=0.75)
    trader._client = OptBroker([_opt_oco()], **kw)
    r = trader.set_trail(OPT, "$", "0.15", "MARK", [], set(), None)
    assert not r["ok"] and "Nothing was sent" in r["error"] and trader._client.placed == []
    assert "did not confirm" in r["error"]                    # unclear, not "Schwab does not accept"


def test_stock_trail_is_not_previewed(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_oco()])
    trader._client.preview_order = lambda *a: pytest.fail("a stock trail must not need a preview")
    assert trader.set_trail("MXR", "$", 2.5, "MARK", [], set(), None)["ok"]


def test_option_auto_breakeven_rounds_up_to_the_nickel(trader, monkeypatch):
    _opt_quote(trader, monkeypatch, bid=0.75, ask=0.80)
    tree = {"orderId": 40, "orderStrategyType": "TRIGGER", "status": "FILLED", "filledQuantity": 4,
            "orderLegCollection": [{"instruction": "BUY_TO_OPEN", "positionEffect": "OPENING", "quantity": 4,
                                    "instrument": {"symbol": OPT, "assetType": "OPTION"}}],
            "childOrderStrategies": [_opt_oco()]}
    trader._client = OptBroker([tree])
    trader.store.insert_ticket({"ticket_id": "t-opt", "symbol": OPT, "side": "LONG", "qty": 4, "limit": "0.52",
                                "stop": "0.30", "target": "0.95", "risk": 88.0, "request": {}})
    trader.store.update_ticket("t-opt", "ACCEPTED", order_id=40)
    trader.store.add_auto_be("t-opt", OPT)
    assert trader.store.set_auto_be("t-opt", "SENDING", only_from="PENDING")
    r = trader.auto_breakeven("t-opt", OPT, "LONG", 0.52, [], set())
    assert r["ok"] and r["state"] == "DONE", r
    (oid, body), = trader._client.replaced
    assert oid == 51 and body["stopPrice"] == "0.55"          # 0.52 is off the grid: up, never below entry


def test_quote_reads_the_option_fields(tmp_path):
    from companion.bracket import EQUITY, OPT_NICKEL, OPT_PENNY, OPT_PENNY_ALL
    t = Trader(Store(tmp_path / "q.db"))

    class Q(FakeClient):
        def quotes(self, syms):
            return JsonResp(200, {
                OPT: {"assetMainType": "OPTION", "realtime": True,
                      "quote": {"bidPrice": 0.55, "askPrice": 0.6, "lastPrice": 0.57, "mark": 0.575},
                      "reference": {"underlying": "AAPL", "isPennyPilot": True, "multiplier": 100.0}},
                "XYZ   261120P00050000": {"assetMainType": "OPTION", "quote": {"bidPrice": 3.1, "askPrice": 3.3},
                                         "reference": {"underlying": "XYZ", "isPennyPilot": False}},
                "SPY   261120C00600000": {"assetMainType": "OPTION", "quote": {"bidPrice": 4.0, "askPrice": 4.02},
                                         "reference": {"isPennyPilot": True}},
                "MXR": {"assetMainType": "EQUITY", "quote": {"bidPrice": 906.0, "askPrice": 906.2}}})
    t._client, t._hash = Q(), "H" * 64
    q = t.quote(OPT)
    assert q["mark"] == 0.575 and q["multiplier"] == 100.0 and q["underlying"] == "AAPL" and q["ticks"] == OPT_PENNY
    assert t.quote("XYZ   261120P00050000")["ticks"] == OPT_NICKEL
    assert t.quote("SPY   261120C00600000")["ticks"] == OPT_PENNY_ALL     # underlying from the OCC root
    assert t.quote("MXR")["ticks"] == EQUITY
    assert t._ticks("MXR", "EQUITY") == EQUITY and t._ticks(OPT, "OPTION") == OPT_PENNY


# ---- option entries from the ticket ------------------------------------------------------------

CALL = "AAPL  261120C00232500"


def _opt_ticket_quote(trader, monkeypatch, bid=0.77, ask=0.79, **extra):
    from companion.bracket import ticks_for
    q = {"bid": bid, "ask": ask, "mark": (bid + ask) / 2, "asset_type": "OPTION", "underlying": "AAPL",
         "multiplier": 100.0, "is_penny": True, "deliverables": "100 AAPL", "ticks": ticks_for("OPTION", "AAPL", True)}
    q.update(extra)
    monkeypatch.setattr(trader, "quote", lambda sym, max_age=1.0: q)


def opt_req(**kw):
    base = {"ticket_id": "opt-ticket-01", "symbol": CALL, "side": "LONG", "stop": "0.50", "risk": 375, "r": 2,
            "entry": "mid", "expected_limit": "0.78", "expected_qty": 13, "expected_target": "1.34"}
    return {**base, **kw}


def test_option_ticket_sends_a_buy_to_open_bracket(trader, monkeypatch):
    _opt_ticket_quote(trader, monkeypatch)
    r = trader.submit(opt_req())
    assert r["ok"], r
    (order,) = trader._client.placed
    leg = order["orderLegCollection"][0]
    assert leg == {"instruction": "BUY_TO_OPEN", "quantity": 13, "instrument": {"symbol": CALL, "assetType": "OPTION"}}
    assert order["price"] == "0.78"
    t = trader.store.get_ticket("opt-ticket-01")
    assert t["limit_price"] == "0.78" and t["risk"] == 364.0          # 13 x 0.28 x 100


def test_option_ticket_lock_uses_option_ticks(trader, monkeypatch):
    # 1R = 0.28: the lock is max(0.028, 2 penny ticks) = 0.028. The mid moving up 2c still sends.
    _opt_ticket_quote(trader, monkeypatch, bid=0.79, ask=0.81)
    assert trader.submit(opt_req())["ok"]
    _opt_ticket_quote(trader, monkeypatch, bid=0.81, ask=0.83)       # mid 0.82: 4c against, refused
    r = trader.submit(opt_req(ticket_id="opt-ticket-02"))
    assert not r["ok"] and "Price moved: entry is now 0.82" in r["error"]


def test_option_ticket_size_message_says_contracts(trader, monkeypatch):
    _opt_ticket_quote(trader, monkeypatch)
    r = trader.submit(opt_req(expected_qty=14))
    assert not r["ok"] and "13 contracts" in r["error"]


def test_option_ticket_refuses_an_adjusted_contract(trader, monkeypatch):
    _opt_ticket_quote(trader, monkeypatch, deliverables="100 AAPL, 50 XYZ")
    r = trader.submit(opt_req())
    assert not r["ok"] and "non-standard" in r["error"] and trader._client.placed == []
    _opt_ticket_quote(trader, monkeypatch, multiplier=10.0)
    assert "non-standard" in trader.make_plan(opt_req()).errors[0]


def test_adjusted_contract_check_fails_closed():
    from companion.trading import _odd_deliverable
    std = {"underlying": "AAPL"}
    assert _odd_deliverable({**std, "deliverables": "100 AAPL"}, CALL) is None
    assert _odd_deliverable({**std, "deliverables": "AAPL 100"}, CALL) is None
    assert _odd_deliverable({**std}, CALL) is None and _odd_deliverable({**std, "deliverables": " "}, CALL) is None
    assert _odd_deliverable({"underlying": "$SPX", "deliverables": "100 $SPX"}, "SPXW  261120C06000000") is None
    # Cash-settled index: Schwab's real format (2026-11-02).
    assert _odd_deliverable({"underlying": "$SPX", "deliverables": "100 $SPX(Cash)"}, "SPXW  261102P06800000") is None
    for d in ("$USD 1191.0;IBM 100", "IBM 100", "150 AAPL", "100 AAPL, 50 XYZ", "100 MSFT", ["100 AAPL"], {"x": 1},
              "100 MSFT(Cash)", "150 AAPL(Cash)"):
        assert _odd_deliverable({**std, "deliverables": d}, CALL), d          # no crash, refused
    assert "adjusted root AAPL1" in _odd_deliverable({**std, "deliverables": "100 AAPL"}, "AAPL1 261120C00232500")


def test_option_ticket_shows_why_a_contract_is_non_standard(trader, monkeypatch):
    _opt_ticket_quote(trader, monkeypatch, deliverables="$USD 1191.0;IBM 100")
    p = trader.make_plan(opt_req())
    assert not p.ok and "non-standard contract" in p.errors[0] and "$USD 1191.0;IBM 100" in p.errors[0]
    _opt_ticket_quote(trader, monkeypatch, deliverables=["100 AAPL"])      # an unexpected type: refused, no crash
    assert not trader.make_plan(opt_req()).ok
    r = trader.submit(opt_req())
    assert not r["ok"] and trader._client.placed == []


def test_option_ticket_with_a_trail_asks_the_preview_first(trader, monkeypatch):
    _opt_ticket_quote(trader, monkeypatch)
    trader._client.preview_order = lambda h, o: JsonResp(200, {"orderValidationResult": {"rejects": [
        {"message": "Trailing stops are not supported for this security."}]}})
    req = opt_req(stop_type="trail", trail_unit="$", trail_value=0.10, trail_basis="MARK",
                  expected_trail_offset=0.10, expected_trail_start="0.68")
    r = trader.submit(req)
    assert not r["ok"] and "does not accept a trailing stop" in r["error"] and "Nothing was sent" in r["error"]
    assert trader._client.placed == [] and trader.store.get_ticket("opt-ticket-01") is None
    trader._client.preview_order = lambda h, o: JsonResp(200, {"orderValidationResult": {"rejects": []}})
    r = trader.submit(req)
    assert r["ok"], r
    stop = trader._client.placed[0]["childOrderStrategies"][0]["childOrderStrategies"][1]
    assert stop["orderType"] == "TRAILING_STOP" and stop["stopPriceOffset"] == 0.1


def test_option_autobe_ticket_is_recorded(trader, monkeypatch):
    _opt_ticket_quote(trader, monkeypatch)
    r = trader.submit(opt_req(stop_type="autobe"))
    assert r["ok"] and r["plan"]["be_trigger"] == "1.06"              # 0.78 + 1R (0.28)
    assert trader.store.auto_be("opt-ticket-01")["state"] == "PENDING"


def test_option_ticket_shows_its_readable_name(trader, monkeypatch):
    _opt_ticket_quote(trader, monkeypatch)
    trader.submit(opt_req())
    (row,) = trader.brackets([], [], {}, True)
    assert row["asset_type"] == "OPTION" and row["display"] == "AAPL 20 Nov 26 232.5C"


def test_parse_chain_cleans_schwab_values():
    from companion.trading import parse_chain
    body = {"underlyingPrice": 229.84, "underlying": {"description": "Apple Inc"}, "callExpDateMap": {"2026-11-20:20": {
        "235.0": [{"symbol": "AAPL  261120C00235000", "strikePrice": 235.0, "bid": 0.40, "ask": 0.42, "mark": 0.41,
                   "delta": 0.12, "volatility": 27.1, "openInterest": 6200, "totalVolume": 900, "pennyPilot": True,
                   "multiplier": 100.0, "nonStandard": False, "inTheMoney": False, "daysToExpiration": 20}],
        "230.0": [{"symbol": "AAPL  261120C00230000", "strikePrice": 230.0, "bid": 1.38, "ask": 1.41, "mark": 1.40,
                   "delta": -999.0, "volatility": 7805.0, "openInterest": 12000, "pennyPilot": True,
                   "multiplier": 100.0, "nonStandard": False, "inTheMoney": True}],
        "100.0": [{"symbol": "AAPL1 261120C00100000", "strikePrice": 100.0, "bid": 129.0, "ask": 131.0,
                   "multiplier": 100.0, "nonStandard": True}]}}}
    out = parse_chain(body, "CALL")
    assert out["underlying_price"] == 229.84 and out["underlying_name"] == "Apple Inc"
    assert [r["strike"] for r in out["rows"]] == [100.0, 230.0, 235.0]          # by strike
    adj, atm, otm = out["rows"]
    assert adj["standard"] is False and atm["standard"] and otm["penny"]
    assert atm["delta"] is None and atm["iv"] is None                           # -999 and junk IV hidden
    assert otm["delta"] == 0.12 and otm["spread_pct"] == 4.9
    assert parse_chain(body, "PUT")["rows"] == []


class ChainClient(FakeClient):
    def __init__(self):
        super().__init__()
        self.calls = []

    def option_expiration_chain(self, sym):
        self.calls.append(("exp", sym))
        return JsonResp(200, {"expirationList": [
            {"expirationDate": "2026-11-06", "daysToExpiration": 6, "expirationType": "W", "standard": True},
            {"expirationDate": "2026-11-02", "daysToExpiration": 2, "expirationType": "W", "standard": True}]})

    def option_chains(self, sym, **kw):
        self.calls.append(("chain", sym, kw))
        return JsonResp(200, {"underlyingPrice": 229.84, "callExpDateMap": {}})


def test_expirations_and_chain_are_validated_and_cached(trader):
    trader._client = ChainClient()
    exps = trader.expirations("aapl")
    assert [e["date"] for e in exps] == ["2026-11-02", "2026-11-06"]
    trader.expirations("AAPL")
    out = trader.chain("AAPL", "2026-11-02", "call")
    trader.chain("AAPL", "2026-11-02", "CALL")
    assert out["put_call"] == "CALL" and out["rows"] == []
    kinds = [c[0] for c in trader._client.calls]
    assert kinds == ["exp", "chain"]                                            # second calls came from the cache
    kw = trader._client.calls[1][2]
    assert kw["contractType"] == "CALL" and kw["fromDate"] == kw["toDate"] == "2026-11-02"
    for bad in (("AAPL; DROP", "2026-11-02", "CALL"), ("AAPL", "next friday", "CALL"), ("AAPL", "2026-11-02", "BOTH")):
        with pytest.raises(ValueError):
            trader.chain(*bad)


def test_index_underlyings_get_schwabs_dollar_prefix(trader):
    # A bare SPX on /chains is a 400 at Schwab (2026-11-02); $SPX typed by hand stays as it is.
    trader._client = ChainClient()
    trader.expirations("spx")
    trader.chain("SPX", "2026-11-02", "PUT")
    trader.chain("$NDX", "2026-11-02", "PUT")
    trader.chain("SPY", "2026-11-02", "PUT")
    assert [c[1] for c in trader._client.calls] == ["$SPX", "$SPX", "$NDX", "SPY"]


def test_only_the_order_client_may_read_option_chains():
    from companion import schwab
    assert {"option_chains", "option_expiration_chain"} <= schwab._TRADING
    assert not ({"option_chains", "option_expiration_chain"} & schwab._ALLOWED)
    assert trading.OCC_RE.match(CALL) and trading.OCC_RE.match("SPY   261120P00600000")
    assert not trading.OCC_RE.match("AAPL")


# ---- size by quantity, no stop, no target ------------------------------------------------------

def qty_req(**kw):
    # Fixture quote 338.70 / 338.77: the mid entry is 338.74.
    base = {"ticket_id": "qty-ticket-01", "symbol": "BRVO", "side": "LONG", "stop": "336.00", "risk": 0, "r": 2,
            "entry": "mid", "size_mode": "qty", "qty": 50, "targets": 0,
            "expected_limit": "338.74", "expected_qty": 50, "expected_target": None}
    return {**base, **kw}


def test_quantity_ticket_with_stop_only(trader):
    r = trader.submit(qty_req())
    assert r["ok"], r
    (order,) = trader._client.placed
    assert order["orderStrategyType"] == "TRIGGER" and order["orderLegCollection"][0]["quantity"] == 50
    (stop,) = order["childOrderStrategies"]
    assert stop["orderType"] == "STOP" and stop["stopPrice"] == "336.00"
    t = trader.store.get_ticket("qty-ticket-01")
    assert t["target"] == "" and t["stop"] == "336.00" and t["risk"] == 137.0      # 50 x 2.74


def test_no_stop_ticket_needs_the_confirmation(trader):
    req = qty_req(stop="", stop_type="none")
    r = trader.submit(req)
    assert not r["ok"] and "confirmation" in r["error"] and trader._client.placed == []
    r = trader.submit({**req, "confirm_no_stop": True})
    assert r["ok"], r
    (order,) = trader._client.placed
    assert order["orderStrategyType"] == "SINGLE" and "childOrderStrategies" not in order
    t = trader.store.get_ticket("qty-ticket-01")
    assert t["stop"] == "" and t["target"] == ""
    assert trader.r_basis([]) == {}                     # no stop, no R


def _stopless_fill(trader):
    assert trader.submit(qty_req(stop="", stop_type="none", confirm_no_stop=True))["ok"]
    oid = trader.store.get_ticket("qty-ticket-01")["order_id"]
    return {"orderId": oid, "status": "FILLED", "orderStrategyType": "SINGLE", "orderActivityCollection": [
        {"executionType": "FILL", "executionLegs": [{"quantity": 50, "price": 338.74,
                                                     "time": "2099-01-01T14:00:20+0000"}]}]}


def _stop_at(trader, stop, oid, created):
    trader.store.record_stop("BRVO", stop, oid)
    trader.store._con.execute("update companion_stops set created=? where order_id=?", (created, oid))


def _stop_order(oid, status="WORKING", stop=337.74):
    return {"orderId": oid, "status": status, "orderStrategyType": "SINGLE", "orderType": "STOP",
            "stopPrice": stop}


def test_no_stop_ticket_takes_r_from_the_first_stop_set_after_the_fill(trader):
    filled = _stopless_fill(trader)
    _stop_at(trader, "339.50", 1, "2099-01-01T14:00:30.000+00:00")   # above a long entry: not a risk stop
    assert trader.r_basis([filled, _stop_order(1, stop=339.5)]) == {}
    _stop_at(trader, "339.50", 1, "2099-01-01T14:00:10.000+00:00")   # moved before the fill
    _stop_at(trader, "337.74", 2, "2099-01-01T14:01:00.000+00:00")
    _stop_at(trader, "338.00", 3, "2099-01-01T14:02:00.000+00:00")   # a later stop never redefines R
    orders = [filled, _stop_order(2, "REPLACED"), _stop_order(3, stop=338.0)]
    b = trader.r_basis(orders)["BRVO"]
    assert b["stop"] == 337.74 and abs(b["rps"] - 1.0) < 1e-9 and b["entry"] == 338.74


def test_stopless_r_skips_a_rejected_stop_and_the_fill_second(trader):
    filled = _stopless_fill(trader)
    _stop_at(trader, "337.00", 1, "2099-01-01T14:00:20.500+00:00")   # same second as the fill: the trade before
    _stop_at(trader, "337.24", 2, "2099-01-01T14:00:40.000+00:00")   # an OCO whose stop leg Schwab rejected
    _stop_at(trader, "337.74", 3, "2099-01-01T14:01:00.000+00:00")
    oco = {"orderId": 2, "status": "WORKING", "orderStrategyType": "OCO", "childOrderStrategies": [
        {"orderId": 21, "status": "WORKING", "orderType": "LIMIT", "price": 340.0},
        _stop_order(22, "REJECTED", 337.24)]}
    orders = [filled, _stop_order(1, stop=337.0), oco, _stop_order(3)]
    assert trader.r_basis(orders)["BRVO"]["stop"] == 337.74


def test_stopless_r_waits_for_the_first_stop_to_show_working(trader):
    filled = _stopless_fill(trader)
    _stop_at(trader, "337.74", 1, "2099-01-01T14:01:00.000+00:00")   # accepted, not seen yet
    _stop_at(trader, "338.24", 2, "2099-01-01T14:02:00.000+00:00")   # a later rebuild after a move
    assert trader.r_basis([filled, _stop_order(2, stop=338.24)]) == {}            # no R, not the later stop
    assert trader.r_basis([filled, _stop_order(1, "ACCEPTED"), _stop_order(2, stop=338.24)]) == {}
    b = trader.r_basis([filled, _stop_order(1), _stop_order(2, stop=338.24)])["BRVO"]
    assert b["stop"] == 337.74


def test_stopless_r_stays_frozen_once_shown(trader):
    filled = _stopless_fill(trader)
    _stop_at(trader, "337.74", 1, "2099-01-01T14:01:00.000+00:00")
    _stop_at(trader, "338.24", 2, "2099-01-01T14:02:00.000+00:00")
    oco = {"orderId": 1, "status": "WORKING", "orderStrategyType": "OCO", "childOrderStrategies": [
        {"orderId": 11, "status": "ACCEPTED", "orderType": "LIMIT", "price": 340.0},
        _stop_order(12, "AWAITING_STOP_CONDITION")]}
    assert trader.r_basis([filled, oco])["BRVO"]["stop"] == 337.74
    oco["childOrderStrategies"][0]["status"] = "REJECTED"           # the target leg, later
    assert trader.r_basis([filled, oco, _stop_order(2, stop=338.24)])["BRVO"]["stop"] == 337.74
    assert trader.r_basis([filled])["BRVO"]["stop"] == 337.74       # gone from the listing


def test_a_set_stop_is_recorded_as_soon_as_schwab_accepts_it(trader, monkeypatch):
    _quote_mark(trader, monkeypatch)
    trader._client = Broker([_mu_trailing()])
    monkeypatch.setattr(trader, "_fresh_orders", lambda client, orders=None: [])   # never shows up
    r = trader.move_stop("MXR", "910.00", [], set())
    assert trader._client.placed and "Not confirmed" in r.get("message", r.get("error", ""))
    assert [st for st, _ in trader.store.stops_since("MXR", "2000-01-01")] == ["910.00"]


def test_no_stop_lock_is_a_tenth_of_a_percent(trader, monkeypatch):
    req = qty_req(stop="", stop_type="none", confirm_no_stop=True)
    _quote(monkeypatch, trader, 338.80, 338.95)           # mid 338.875: 0.135 against, under 0.1% (0.339)
    assert trader.submit(req)["ok"]
    _quote(monkeypatch, trader, 339.05, 339.20)           # mid 339.125: 0.385 against, refused
    r = trader.submit({**req, "ticket_id": "qty-ticket-02"})
    assert not r["ok"] and "Price moved" in r["error"]


def test_quantity_ticket_refuses_a_changed_quantity_or_target(trader):
    r = trader.submit(qty_req(expected_qty=40))
    assert not r["ok"] and "now says 50 shares" in r["error"]
    r = trader.submit(qty_req(targets=1))                 # the plan has a target, the page showed none
    assert not r["ok"] and "ticket showed none" in r["error"]
    r = trader.submit(qty_req(expected_target="344.22"))  # the page showed a target, the plan has none
    assert not r["ok"] and "plan has none" in r["error"]
    assert trader._client.placed == []


def test_target_only_ticket(trader):
    r = trader.submit(qty_req(stop="", stop_type="none", targets=1, target_price="342.00",
                              expected_target="342.00", confirm_no_stop=True))
    assert r["ok"], r
    (tgt,) = trader._client.placed[0]["childOrderStrategies"]
    assert tgt["orderType"] == "LIMIT" and tgt["price"] == "342.00"


def test_reconcile_matches_the_new_order_shapes():
    from companion.bracket import bracket_order, plan
    kw = dict(symbol="NVDA", side="LONG", stop="186.50", risk=0, r_multiple=2, bid="188.62", ask="188.68",
              entry="mid", size_mode="qty", qty=100)

    def as_schwab(o):
        """What Schwab reports back: ids, statuses, numbers instead of strings."""
        import copy
        o = copy.deepcopy(o)
        ids = iter(range(100, 200))

        def walk(n):
            n["orderId"], n["status"] = next(ids), "WORKING"
            for k in ("price", "stopPrice"):
                if k in n:
                    n[k] = float(n[k])
            for ch in n.get("childOrderStrategies") or []:
                walk(ch)
        walk(o)
        return o
    for p in (plan(**kw, target=False), plan(**{**kw, "stop_type": "none"}, target=False),
              plan(**{**kw, "stop_type": "none"}, target_price="195.00")):
        sent = bracket_order(p)
        assert Trader._matches(as_schwab(sent), {"request": sent}), sent
    a, b = bracket_order(plan(**kw, target=False)), bracket_order(plan(**{**kw, "qty": 99}, target=False))
    assert not Trader._matches(as_schwab(a), {"request": b})


def test_typed_limit_does_not_follow_the_quote(trader, monkeypatch):
    req = {"ticket_id": "lmt-ticket-01", "symbol": "BRVO", "side": "LONG", "stop": "336.00", "risk": 500, "r": 2,
           "entry": "limit", "limit_price": "338.00", "expected_limit": "338.00", "expected_qty": 250,
           "expected_target": "342.00"}
    _quote(monkeypatch, trader, 339.40, 339.50)          # the market moved up: your limit stays your limit
    r = trader.submit(req)
    assert r["ok"], r
    assert trader._client.placed[0]["price"] == "338.00"
    _quote(monkeypatch, trader, 335.90, 336.00)          # now the stop is at the bid: refused, nothing sent
    r = trader.submit({**req, "ticket_id": "lmt-ticket-02"})
    assert not r["ok"] and len(trader._client.placed) == 1


def _mu_target_only():
    return _leaf(70, "LIMIT", 57, price=900.18)


def test_partial_on_a_target_only_position_puts_the_target_back(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_mu_target_only()])              # short 57, target only, no stop
    r = trader.take_partial("MXR", 0.5, None, [], set(), mode="market")
    partial, target = trader._client.placed
    assert partial["orderType"] == "MARKET" and partial["orderLegCollection"][0]["quantity"] == 28
    assert target["orderStrategyType"] == "SINGLE" and target["orderType"] == "LIMIT" and target["price"] == "900.18"
    assert target["orderLegCollection"][0] == {"instruction": "BUY_TO_COVER", "quantity": 29,
                                               "instrument": {"symbol": "MXR", "assetType": "EQUITY"}}
    assert not r["ok"] and "put back on the 29" in r["error"] and "NO STOP" in r["error"]


def test_partial_on_a_target_only_position_says_when_the_target_is_lost(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    broker = Broker([_mu_target_only()])
    real = broker.place_order
    broker.place_order = lambda h, o: Resp(400, text="bad price") if o.get("orderType") == "LIMIT" else real(h, o)
    trader._client = broker
    r = trader.take_partial("MXR", 0.5, None, [], set(), mode="market")
    assert not r["ok"] and "target is GONE" in r["error"]


def test_partial_target_only_with_a_server_error_says_it_may_be_working(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    broker = Broker([_mu_target_only()])
    real = broker.place_order
    broker.place_order = lambda h, o: Resp(500, text="boom") if o.get("orderType") == "LIMIT" else real(h, o)
    trader._client = broker
    r = trader.take_partial("MXR", 0.5, None, [], set(), mode="market")
    assert not r["ok"] and "MAY be working" in r["error"] and "GONE" not in r["error"]


# ---- leg guard: Close / partial on one leg of a spread or on covered shares ----------------

LONG_C, SHORT_C = "META  261120C00615000", "META  261120C00620000"


def _opt(sym, qty, put_call="CALL", und="META"):
    return {"instrument": {"symbol": sym, "assetType": "OPTION", "putCall": put_call, "underlyingSymbol": und},
            "longQuantity": max(qty, 0), "shortQuantity": max(-qty, 0)}


def _call_leg(qty, others, orders=()):
    return Broker(list(orders), position=(LONG_C if qty > 0 else SHORT_C, qty, "OPTION"),
                  ins={"putCall": "CALL", "underlyingSymbol": "META"}, others=others)


def test_closing_the_long_leg_of_a_call_spread_asks_first(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = _call_leg(2.0, [_opt(SHORT_C, -2.0)])
    r = trader.close_position(LONG_C, [], set())
    assert not r["ok"] and r["needs_confirm"] and "UNCOVERED" in r["error"] and SHORT_C in r["error"]
    assert trader._client.placed == []
    r = trader.close_position(LONG_C, [], set(), confirm_leg=True)
    (order,) = trader._client.placed
    assert order["orderLegCollection"][0]["instruction"] == "SELL_TO_CLOSE"


def test_closing_the_short_leg_says_the_long_is_left_alone(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = _call_leg(-2.0, [_opt(LONG_C, 2.0)])
    r = trader.close_position(SHORT_C, [], set())
    assert r["needs_confirm"] and "other leg alone" in r["error"] and trader._client.placed == []


def test_closing_shares_under_a_covered_call_asks_first(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([], position=("META", 100.0, "EQUITY"), others=[_opt(SHORT_C, -1.0)])
    r = trader.close_position("META", [], set())
    assert r["needs_confirm"] and "UNCOVERED" in r["error"] and trader._client.placed == []


def test_unrelated_options_do_not_trigger_the_leg_guard(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    others = [_opt("META  261120P00590000", -1.0, "PUT"),                 # a put, not the call's pair
              _opt("AMD   261120C00200000", -1.0, und="AMD"),             # another underlying
              _opt("META  261120C00630000", 1.0)]                         # same side as the closed leg
    trader._client = _call_leg(2.0, others)
    r = trader.close_position(LONG_C, [], set())
    assert r["ok"] and len(trader._client.placed) == 1
    trader._client = Broker([], position=("META", 100.0, "EQUITY"), others=[_opt("META  261120P00590000", -1.0, "PUT")])
    assert trader.close_position("META", [], set())["ok"]                 # a short put is not covered by shares


def test_a_working_spread_order_on_the_leg_asks_first(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    spread = {"orderId": 81, "orderStrategyType": "SINGLE", "status": "WORKING", "filledQuantity": 0,
              "orderLegCollection": [
                  {"instruction": "SELL_TO_CLOSE", "instrument": {"symbol": LONG_C}},
                  {"instruction": "BUY_TO_CLOSE", "instrument": {"symbol": SHORT_C}}]}
    trader._client = _call_leg(2.0, [], orders=[spread])
    r = trader.close_position(LONG_C, [], set())
    assert r["needs_confirm"] and "multi-leg" in r["error"] and trader._client.placed == []


def test_a_partial_on_a_spread_leg_asks_first(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = _call_leg(4.0, [_opt(SHORT_C, -4.0)])
    r = trader.take_partial(LONG_C, 0.5, None, [], set())
    assert r["needs_confirm"] and "UNCOVERED" in r["error"] and trader._client.placed == []
    r = trader.take_partial(LONG_C, 1.0, None, [], set())                # 100% goes through Close's guard
    assert r["needs_confirm"] and trader._client.placed == []
    trader.take_partial(LONG_C, 0.5, None, [], set(), confirm_leg=True)
    assert trader._client.placed and trader._client.placed[0]["orderLegCollection"][0]["quantity"] == 2


def test_leg_guard_falls_back_to_the_contract_symbol(trader, monkeypatch):
    """Schwab fields missing on one side: underlying and call/put come from the OCC symbol,
    and SPXW legs pair with an SPX-underlying leg."""
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    bare = {"instrument": {"symbol": SHORT_C, "assetType": "OPTION"}, "longQuantity": 0, "shortQuantity": 2}
    trader._client = _call_leg(2.0, [bare])
    assert trader.close_position(LONG_C, [], set())["needs_confirm"]
    put = {"instrument": {"symbol": "META  261120P00590000", "assetType": "OPTION"}, "longQuantity": 0,
           "shortQuantity": 1}
    trader._client = Broker([], position=(LONG_C, 2.0, "OPTION"), others=[put])     # no fields at all
    assert trader.close_position(LONG_C, [], set())["ok"]                          # a put: no pair
    trader._client = Broker([], position=("SPXW  261102P07600000", 1.0, "OPTION"),
                            others=[_opt("SPXW  261102P07610000", -1.0, "PUT", und="$SPX")])
    assert trader.close_position("SPXW  261102P07600000", [], set())["needs_confirm"]


def test_short_shares_under_a_short_put_ask_first(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([], position=("META", -100.0, "EQUITY"),
                            others=[_opt("META  261120P00590000", -1.0, "PUT")])
    r = trader.close_position("META", [], set())
    assert r["needs_confirm"] and "UNCOVERED" in r["error"]


def test_a_stock_close_reads_orders_no_more_than_before(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    trader._client = Broker([_mu_oco()])
    calls = []
    real = trader._client.account_orders
    trader._client.account_orders = lambda *a, **k: calls.append(1) or real(*a, **k)
    assert trader.close_position("MXR", list(trader._client.orders.values()), set())["ok"]
    assert len(calls) == 1                                  # only the cleanup read after the close


def test_a_spread_order_pending_cancel_still_asks_first(trader, monkeypatch):
    monkeypatch.setattr(trading.time, "sleep", lambda s: None)
    spread = {"orderId": 82, "orderStrategyType": "SINGLE", "status": "PENDING_CANCEL", "filledQuantity": 0,
              "orderLegCollection": [
                  {"instruction": "SELL_TO_CLOSE", "instrument": {"symbol": LONG_C}},
                  {"instruction": "SELL_TO_CLOSE", "instrument": {"symbol": "META  261120P00590000"}}]}
    trader._client = _call_leg(1.0, [_opt("META  261120P00590000", 1.0, "PUT")], orders=[spread])
    r = trader.close_position(LONG_C, [], set())
    assert r["needs_confirm"] and "multi-leg" in r["error"] and trader._client.placed == []
