from decimal import Decimal

from companion.bracket import bracket_order, plan


def test_long_sizing_from_marketable_limit():
    p = plan("avgo", "LONG", stop="336.00", risk=500, r_multiple=2, bid="338.70", ask="338.77")
    assert p.ok
    assert p.limit == Decimal("338.94")              # 338.77 x 1.0005 = 338.939, rounded up
    assert p.per_share == Decimal("2.94")
    assert p.qty == 170                              # floor(500 / 2.94)
    assert p.planned_risk <= 500
    assert p.target == Decimal("344.82")             # 338.94 + 5.88


def test_short_sizing_and_target():
    p = plan("TSX", "SHORT", stop="424.00", risk=375, r_multiple=2, bid="422.80", ask="422.85")
    assert p.ok
    assert p.limit == Decimal("422.58")              # 422.80 x 0.9995 = 422.5886, rounded down
    assert p.per_share == Decimal("1.42")
    assert p.qty == 264
    assert p.target == Decimal("419.74")


def test_stop_on_wrong_side_is_refused():
    p = plan("BRVO", "LONG", stop="339.00", risk=500, r_multiple=2, bid="338.70", ask="338.77")
    assert not p.ok and "below the bid" in p.errors[0]
    p = plan("BRVO", "SHORT", stop="338.00", risk=500, r_multiple=2, bid="338.70", ask="338.77")
    assert not p.ok and "above the ask" in p.errors[0]


def test_off_tick_stop_is_refused_not_rounded():
    p = plan("BRVO", "LONG", stop="336.005", risk=500, r_multiple=2, bid="338.70", ask="338.77")
    assert not p.ok and p.qty == 0


def test_risk_smaller_than_one_share():
    p = plan("BRVO", "LONG", stop="300.00", risk=10, r_multiple=2, bid="338.70", ask="338.77")
    assert not p.ok and p.qty == 0


def test_notional_cap():
    p = plan("X", "LONG", stop="9.90", risk=50000, r_multiple=2, bid="10.00", ask="10.00")
    assert p.qty * p.limit <= 250000


def test_non_finite_inputs_are_refused():
    p = plan("BRVO", "LONG", stop="336.00", risk=500, r_multiple="Infinity", bid="338.70", ask="338.77")
    assert not p.ok and p.qty == 0


def test_short_target_below_zero_is_refused():
    p = plan("X", "SHORT", stop="16.00", risk=500, r_multiple=2, bid="10.00", ask="10.00")
    assert not p.ok and "profitable side" in p.errors[0]


def test_bracket_order_shape_long():
    p = plan("BRVO", "LONG", stop="336.00", risk=500, r_multiple=2, bid="338.70", ask="338.77")
    o = bracket_order(p)
    assert o["orderStrategyType"] == "TRIGGER" and o["price"] == "338.94"
    assert o["orderLegCollection"][0] == {"instruction": "BUY", "quantity": 170,
                                          "instrument": {"symbol": "BRVO", "assetType": "EQUITY"}}
    (oco,) = o["childOrderStrategies"]
    target, stop = oco["childOrderStrategies"]
    assert oco["orderStrategyType"] == "OCO"
    assert target["orderType"] == "LIMIT" and target["price"] == "344.82"
    assert stop["orderType"] == "STOP" and stop["stopPrice"] == "336.00"
    assert {c["orderLegCollection"][0]["instruction"] for c in (target, stop)} == {"SELL"}
    assert all(c["orderLegCollection"][0]["quantity"] == 170 for c in (target, stop))


def test_two_target_plan_and_order():
    p = plan("BRVO", "LONG", stop="336.00", risk=500, r_multiple=2, bid="338.70", ask="338.77",
             split=0.5, t1_r=1)
    assert p.ok and p.qty == 170 and p.qty1 == 85
    assert p.target1 == Decimal("341.88")              # 338.94 + 2.94
    assert p.target == Decimal("344.82")
    o = bracket_order(p)
    first, runner = o["childOrderStrategies"]
    assert o["orderLegCollection"][0]["quantity"] == 170
    for oco, qty, tgt in ((first, 85, "341.88"), (runner, 85, "344.82")):
        assert oco["orderStrategyType"] == "OCO"
        target, stop = oco["childOrderStrategies"]
        assert target["price"] == tgt and stop["stopPrice"] == "336.00"
        assert {c["orderLegCollection"][0]["quantity"] for c in (target, stop)} == {qty}


def test_two_target_odd_split_and_short():
    p = plan("TSX", "SHORT", stop="424.00", risk=375, r_multiple=3, bid="422.80", ask="422.85",
             split=0.5, t1_r=1.5)
    assert p.ok and p.qty == 264 and p.qty1 == 132 and p.target1 == Decimal("420.45")
    p = plan("X", "LONG", stop="9.00", risk=10, r_multiple=2, bid="10.00", ask="10.00", split=0.5, t1_r=1)
    assert p.ok and (p.qty, p.qty1) == (9, 4)                      # odd size: runner gets the extra share
    p = plan("X", "LONG", stop="9.00", risk=1.5, r_multiple=2, bid="10.00", ask="10.00", split=0.5, t1_r=1)
    assert not p.ok and "cannot be split" in p.errors[0]           # 1 share cannot be split
    p = plan("BRVO", "LONG", stop="336.00", risk=500, r_multiple=2, bid="338.70", ask="338.77",
             split=0.5, t1_r=2)
    assert not p.ok and "between the entry and the runner" in p.errors[0]


def test_bracket_order_shape_short():
    p = plan("TSX", "SHORT", stop="424.00", risk=375, r_multiple=2, bid="422.80", ask="422.85")
    o = bracket_order(p)
    assert o["orderLegCollection"][0]["instruction"] == "SELL_SHORT"
    target, stop = o["childOrderStrategies"][0]["childOrderStrategies"]
    assert target["orderLegCollection"][0]["instruction"] == "BUY_TO_COVER"
    assert stop["stopPrice"] == "424.00"


def test_entry_modes_price_the_limit():
    q = dict(stop="336.00", risk=500, r_multiple=2, bid="338.70", ask="338.80")
    assert plan("BRVO", "LONG", entry="mid", **q).limit == Decimal("338.75")
    assert plan("BRVO", "LONG", entry="bid", **q).limit == Decimal("338.70")     # resting
    assert plan("BRVO", "LONG", entry="ask", **q).limit == Decimal("338.80")     # takes the offer
    s = dict(q, stop="341.00")
    assert plan("BRVO", "SHORT", entry="mid", **s).limit == Decimal("338.75")
    assert plan("BRVO", "SHORT", entry="bid", **s).limit == Decimal("338.70")    # hits the bid
    m = plan("BRVO", "LONG", entry="market", **q)
    assert m.ok and m.market and m.limit == Decimal("338.97")                     # sized from ask + 0.05%
    o = bracket_order(m)
    assert o["orderType"] == "MARKET" and "price" not in o
    assert not plan("BRVO", "LONG", entry="stop", **q).ok


def test_target_prices_override_r():
    p = plan("AMZE", "LONG", stop="158.40", risk=375, r_multiple=2, bid="160.10", ask="160.14", entry="mid",
             split=0.5, t1_r=1, target_price="163.50")
    assert p.ok and p.limit == Decimal("160.12") and p.qty == 218
    assert p.target == Decimal("163.50") and p.target1 == Decimal("161.84")
    assert p.as_dict()["target_r"] == 1.97
    p = plan("AMZE", "LONG", stop="158.40", risk=375, r_multiple=2, bid="160.10", ask="160.14", entry="mid",
             split=0.5, t1_price="161.00", target_price="162.50")
    assert p.ok and p.target1 == Decimal("161.00") and p.target == Decimal("162.50")
    bad = plan("AMZE", "LONG", stop="158.40", risk=375, r_multiple=2, bid="160.10", ask="160.14", entry="mid",
               split=0.5, t1_price="163.00", target_price="162.50")
    assert not bad.ok                                                           # target 1 beyond the runner
    assert not plan("AMZE", "LONG", stop="158.40", risk=375, r_multiple=2, bid="160.10", ask="160.14",
                    target_price="159.00").ok                                   # below the entry
    assert not plan("AMZE", "LONG", stop="158.40", risk=375, r_multiple=2, bid="160.10", ask="160.14",
                    target_price="163.505").ok                                  # off tick


def test_sub_penny_quote_is_ticked_before_sizing():
    p = plan("X", "LONG", stop="10.00", risk=100, r_multiple=2, bid="10.026", ask="10.04", entry="bid")
    assert p.limit == Decimal("10.02") and p.qty == 5000                    # sized at the price sent
    assert p.per_share * p.qty <= 100
    a = plan("X", "LONG", stop="10.00", risk=100, r_multiple=2, bid="10.02", ask="10.034", entry="ask")
    assert a.limit == Decimal("10.04") and a.per_share * a.qty <= 100


def test_bad_typed_target_blocks_instead_of_using_r():
    for bad in ("abc", "0", "-1"):
        p = plan("AMZE", "LONG", stop="158.40", risk=375, r_multiple=2, bid="160.10", ask="160.14",
                 entry="mid", target_price=bad)
        assert not p.ok, bad


def test_autobe_bracket_is_a_1r_trailing_stop():
    from companion.bracket import plan, bracket_order
    p = plan("TSX", "LONG", stop="414.73", risk=375, r_multiple=2, bid="417.80", ask="417.86", entry="mid",
             stop_type="autobe")
    assert p.ok and p.trail == {"offset": p.per_share, "link": "VALUE", "basis": "MARK"}
    assert p.trail_start == p.stop and not p.warnings                     # starts exactly at the typed stop
    stop = bracket_order(p)["childOrderStrategies"][0]["childOrderStrategies"][1]
    assert stop["orderType"] == "TRAILING_STOP" and "stopPrice" not in stop
    assert stop["stopPriceOffset"] == float(p.per_share) and stop["stopPriceLinkType"] == "VALUE"


def test_trail_wider_than_the_stop_warns_and_percent_works():
    from companion.bracket import plan
    p = plan("TSX", "LONG", stop="414.73", risk=375, r_multiple=2, bid="417.80", ask="417.86", entry="mid",
             stop_type="trail", trail_unit="R", trail_value=1.5)
    assert p.ok and p.warnings and "more risk" in p.warnings[0]          # warn, never block
    q = plan("TSX", "SHORT", stop="420.00", risk=375, r_multiple=2, bid="417.80", ask="417.86", entry="mid",
             stop_type="trail", trail_unit="%", trail_value=0.5)
    assert q.ok and q.trail["link"] == "PERCENT" and q.trail_start > q.limit
    assert not plan("TSX", "LONG", stop="414.73", risk=375, r_multiple=2, bid="417.80", ask="417.86",
                    stop_type="trail", trail_unit="$", trail_value=0).ok
