from companion.orders import pending_orders, stop_coverage


def leg(symbol, instruction, qty, effect="CLOSING", leg_id=1):
    return {"legId": leg_id, "instruction": instruction, "positionEffect": effect, "quantity": qty,
            "instrument": {"assetType": "EQUITY", "symbol": symbol}}


def order(oid, otype, instruction, qty, stop=None, price=None, status="WORKING", **kw):
    o = {"orderId": oid, "status": status, "orderType": otype, "orderStrategyType": "SINGLE",
         "quantity": qty, "remainingQuantity": kw.get("remaining", qty), "duration": "DAY",
         "orderLegCollection": [leg("BRVO", instruction, qty, kw.get("effect", "CLOSING"))]}
    if stop is not None:
        o["stopPrice"] = stop
    if price is not None:
        o["price"] = price
    return o


def oco(oid, *children):
    return {"orderId": oid, "status": "WORKING", "orderStrategyType": "OCO",
            "childOrderStrategies": list(children)}


def test_oco_stops_are_alternatives_not_summed():
    pend = pending_orders([oco(1, order(2, "STOP", "SELL", 100, stop=90),
                               order(3, "STOP", "SELL", 100, stop=95))])
    covered, alloc = stop_coverage(pend, "BRVO", "LONG", 100)
    assert covered == 100 and len(alloc) == 1


def test_alternative_stops_in_oco_nested_under_trigger_count_once():
    entry = order(1, "LIMIT", "BUY", 100, price=100, status="FILLED", effect="OPENING")
    entry["orderStrategyType"] = "TRIGGER"
    entry["childOrderStrategies"] = [oco(2, order(3, "STOP", "SELL", 100, stop=95),
                                         order(4, "STOP", "SELL", 100, stop=96))]
    covered, alloc = stop_coverage(pending_orders([entry]), "BRVO", "LONG", 150)
    assert covered == 100 and len(alloc) == 1


def test_wrong_side_stop_does_not_protect():
    pend = pending_orders([order(1, "STOP", "BUY", 100, stop=110)])
    covered, _ = stop_coverage(pend, "BRVO", "LONG", 100)
    assert covered == 0


def test_independent_stops_add_up_and_cap_at_position():
    pend = pending_orders([order(1, "STOP", "SELL", 60, stop=90), order(2, "STOP", "SELL", 60, stop=89)])
    covered, alloc = stop_coverage(pend, "BRVO", "LONG", 100)
    assert covered == 100 and sum(q for _, q in alloc) == 100


def test_remaining_zero_is_not_original_quantity():
    pend = pending_orders([order(1, "STOP", "SELL", 100, stop=90, remaining=0)])
    assert pend[0].qty == 0


def test_manual_review_and_awaiting_parent_do_not_protect():
    pend = pending_orders([order(1, "STOP", "SELL", 100, stop=90, status="AWAITING_MANUAL_REVIEW"),
                           order(2, "STOP", "SELL", 100, stop=90, status="AWAITING_PARENT_ORDER")])
    assert stop_coverage(pend, "BRVO", "LONG", 100)[0] == 0


def test_order_frozen_at_working_but_filled_inside_oco_is_not_pending():
    # Live 2026-10-20: MXR stop 1008370363036 listed alone as WORKING and inside an OCO as FILLED.
    lone = order(5, "STOP", "SELL", 75, stop=904.23)
    tree = oco(6, order(5, "STOP", "SELL", 75, stop=904.23, status="FILLED"),
               order(7, "LIMIT", "SELL", 75, price=911.5, status="CANCELED"))
    assert pending_orders([lone, tree]) == []
    assert pending_orders([tree, lone]) == []


def test_multi_leg_order_emits_every_leg():
    o = {"orderId": 9, "status": "WORKING", "orderType": "NET_DEBIT", "orderStrategyType": "SINGLE",
         "quantity": 1, "price": 1.25, "duration": "DAY",
         "orderLegCollection": [leg("SPY   261120C00600000", "BUY_TO_OPEN", 1, "OPENING", 1),
                                leg("SPY   261120C00610000", "SELL_TO_OPEN", 1, "OPENING", 2)]}
    rows = pending_orders([o])
    assert [(r.leg_id, r.legs) for r in rows] == [(1, 2), (2, 2)]
