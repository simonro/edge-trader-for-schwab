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


def test_autobe_bracket_is_a_fixed_stop_with_a_1r_trigger():
    from companion.bracket import plan, bracket_order
    p = plan("TSX", "LONG", stop="414.73", risk=375, r_multiple=2, bid="417.80", ask="417.86", entry="mid",
             stop_type="autobe")
    assert p.ok and p.trail is None and p.be_trigger == p.limit + p.per_share
    stop = bracket_order(p)["childOrderStrategies"][0]["childOrderStrategies"][1]
    assert stop["orderType"] == "STOP" and stop["stopPrice"] == "414.73"
    q = plan("TSX", "SHORT", stop="420.00", risk=375, r_multiple=2, bid="417.80", ask="417.86", entry="mid",
             stop_type="autobe")
    assert q.be_trigger == q.limit - q.per_share


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


# ---- option price grids ------------------------------------------------------------------

def test_ticks_for_picks_the_grid():
    from companion.bracket import EQUITY, OPT_NICKEL, OPT_PENNY, OPT_PENNY_ALL, ticks_for
    assert ticks_for("EQUITY") == EQUITY and ticks_for(None) == EQUITY
    assert ticks_for("OPTION", "AAPL", True) == OPT_PENNY
    assert ticks_for("OPTION", "XYZ", False) == OPT_NICKEL
    assert ticks_for("OPTION", "XYZ", None) == OPT_NICKEL        # unknown flag: the grid valid everywhere
    assert ticks_for("OPTION", "SPY", False) == OPT_PENNY_ALL     # SPY / QQQ / IWM: pennies at every price


def test_option_ticks_and_rounding():
    from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP
    from companion.bracket import OPT_NICKEL, OPT_PENNY, OPT_PENNY_ALL, _to_tick, fmt, tick
    D = Decimal
    assert tick(D("2.99"), OPT_PENNY) == D("0.01") and tick(D("3.00"), OPT_PENNY) == D("0.05")
    assert tick(D("2.99"), OPT_NICKEL) == D("0.05") and tick(D("3.00"), OPT_NICKEL) == D("0.10")
    assert tick(D("12.34"), OPT_PENNY_ALL) == D("0.01")
    assert _to_tick(D("0.37"), ROUND_HALF_UP, OPT_NICKEL) == D("0.35")
    assert _to_tick(D("0.37"), ROUND_CEILING, OPT_NICKEL) == D("0.40")
    assert _to_tick(D("3.07"), ROUND_FLOOR, OPT_NICKEL) == D("3.00")
    assert _to_tick(D("2.98"), ROUND_CEILING, OPT_NICKEL) == D("3.00")
    assert _to_tick(D("3.02"), ROUND_CEILING, OPT_PENNY) == D("3.05")
    assert _to_tick(D("2.996"), ROUND_CEILING, OPT_PENNY) == D("3.00")
    assert fmt(D("0.35"), OPT_NICKEL) == "0.35"                   # cents, never 4 decimals
    assert fmt(D("0.35")) == "0.3500"                             # stocks under $1 unchanged


def test_trail_offset_on_an_option_grid():
    from companion.bracket import OPT_NICKEL, trail_spec
    spec, err = trail_spec("$", "0.13", None, "MARK", OPT_NICKEL)
    assert not err and spec["offset"] == Decimal("0.15")
    spec, err = trail_spec("$", "0.02", None, "MARK", OPT_NICKEL)
    assert spec is None and "rounds to 0" in err


# ---- option entries ----------------------------------------------------------------------

AAPL_C = "AAPL  261120C00232500"
AAPL_P = "AAPL  261120P00227500"


def _opt(**kw):
    from companion.bracket import OPT_PENNY
    base = dict(symbol=AAPL_C, side="LONG", stop="0.50", risk=375, r_multiple=2, bid="0.77", ask="0.79",
                entry="mid", asset="OPTION", multiplier=100, ticks=OPT_PENNY)
    base.update(kw)
    return plan(**base)


def test_option_sizing_counts_the_multiplier():
    p = _opt()
    assert p.ok, p.errors
    assert p.limit == Decimal("0.78") and p.per_share == Decimal("0.28")
    assert p.qty == 13                                    # floor(375 / 28)
    assert p.planned_risk == Decimal("364.00") and p.notional == Decimal("1014.00")
    assert p.target == Decimal("1.34")                    # 0.78 + 2 x 0.28
    d = p.as_dict()
    assert d["limit"] == "0.78" and d["unit_risk"] == 28.0 and d["asset"] == "OPTION"


def test_option_bracket_json():
    order = bracket_order(_opt())
    assert order["price"] == "0.78" and order["orderType"] == "LIMIT"
    entry = order["orderLegCollection"][0]
    assert entry["instruction"] == "BUY_TO_OPEN" and entry["instrument"] == {"symbol": AAPL_C, "assetType": "OPTION"}
    target, stop = order["childOrderStrategies"][0]["childOrderStrategies"]
    assert target["price"] == "1.34" and stop["stopPrice"] == "0.50"
    assert {target["orderLegCollection"][0]["instruction"], stop["orderLegCollection"][0]["instruction"]} == {"SELL_TO_CLOSE"}


def test_sell_to_open_sizes_from_the_stop_above_and_warns():
    p = _opt(symbol=AAPL_P, side="SHORT", stop="1.60", r_multiple=1, bid="0.84", ask="0.86")
    assert p.ok, p.errors
    assert p.limit == Decimal("0.85") and p.qty == 5 and p.target == Decimal("0.10")
    assert any("Sell to open" in w for w in p.warnings)
    assert not any("assigned" in w for w in p.warnings)  # a put: no early-assignment note
    order = bracket_order(p)
    assert order["orderLegCollection"][0]["instruction"] == "SELL_TO_OPEN"
    assert order["childOrderStrategies"][0]["childOrderStrategies"][1]["orderLegCollection"][0]["instruction"] == "BUY_TO_CLOSE"
    call = _opt(side="SHORT", stop="1.20", r_multiple=1)
    assert any("assigned" in w for w in call.warnings)


def test_option_target_below_zero_says_what_to_do():
    p = _opt(symbol=AAPL_P, side="SHORT", stop="1.60", r_multiple=2, bid="0.84", ask="0.86")
    assert not p.ok and "pick a smaller R" in p.errors[0]


def test_option_prices_must_sit_on_the_contract_grid():
    from companion.bracket import OPT_NICKEL
    p = _opt(ticks=OPT_NICKEL, stop="0.52", bid="0.75", ask="0.85")
    assert not p.ok and "0.05 steps" in p.errors[0]
    p = _opt(ticks=OPT_NICKEL, stop="0.50", bid="0.75", ask="0.85")
    assert p.ok and p.limit == Decimal("0.80")            # mid 0.80 on the nickel grid
    assert p.target == Decimal("1.40")                     # 0.80 + 0.60, on the grid
    p = _opt(ticks=OPT_NICKEL, stop="2.50", bid="3.20", ask="3.40", risk=2000)
    assert p.ok and p.limit == Decimal("3.30") and p.target == Decimal("4.90")   # dimes from $3


def test_option_market_entry_and_odd_contracts_are_refused():
    assert "limit" in _opt(entry="market").errors[0]
    assert "limit" in _opt(entry="cross").errors[0]
    assert "non-standard" in _opt(multiplier=10).errors[0]


def test_option_warnings_and_caps():
    wide = _opt(bid="0.70", ask="0.86", stop="0.40")
    assert any("Wide market" in w for w in wide.warnings)
    assert _opt(risk=100000, stop="0.70").qty == 100      # MAX_CONTRACTS
    tiny = _opt(risk=20)
    assert not tiny.ok and "one contract's risk ($28.00)" in tiny.errors[0]


def test_stock_plan_is_unchanged_by_the_option_fields():
    p = plan("avgo", "LONG", stop="336.00", risk=500, r_multiple=2, bid="338.70", ask="338.77")
    assert p.asset == "EQUITY" and p.multiplier == 1 and p.unit_risk == p.per_share
    assert bracket_order(p)["orderLegCollection"][0]["instrument"] == {"symbol": "AVGO", "assetType": "EQUITY"}


# ---- size by quantity, no stop, no target ------------------------------------------------

def _stk(**kw):
    base = dict(symbol="NVDA", side="LONG", stop="186.50", risk=0, r_multiple=2, bid="188.62", ask="188.68",
                entry="mid", size_mode="qty", qty=100)
    base.update(kw)
    return plan(**base)


def test_quantity_mode_sends_the_typed_size_with_risk_shown():
    p = _stk()
    assert p.ok, p.errors
    assert p.qty == 100 and p.limit == Decimal("188.65") and p.per_share == Decimal("2.15")
    assert p.planned_risk == Decimal("215.00") and p.target == Decimal("192.95")
    assert bracket_order(p)["childOrderStrategies"][0]["orderStrategyType"] == "OCO"   # full bracket, as before


def test_quantity_mode_refuses_bad_or_too_big_sizes():
    assert "whole number" in _stk(qty="2.5").errors[0]
    assert "whole number" in _stk(qty=None).errors[0]
    assert "at most 10000" in _stk(qty=10001).errors[0]
    assert "notional cap" in _stk(qty=2000).errors[0]               # 2000 x 188.65 > $250,000
    assert "at most 100 contracts" in _opt(size_mode="qty", qty=101).errors[0]


def test_no_stop_only_when_sizing_by_quantity():
    p = _stk(stop_type="none", stop="186.50", target_price="195.00")
    assert p.ok, p.errors
    assert p.stop == 0 and p.per_share == 0 and p.planned_risk == 0      # the typed stop is ignored
    assert p.warnings[0].startswith("NO STOP")
    assert "sizing by risk needs a stop" in _stk(size_mode="risk", risk=500, stop_type="none").errors[0]
    assert "type a stop price (or pick None)" in _stk(stop="").errors[0]


def test_no_stop_means_no_r():
    assert "type the target price" in _stk(stop_type="none").errors[0]
    assert "two targets need a stop" in _stk(stop_type="none", target_price="195.00", split=0.5).errors[0]
    assert "two targets need a stop" in _stk(target=False, split=0.5).errors[0]


def test_the_four_order_shapes():
    full = bracket_order(_stk())
    stop_only = bracket_order(_stk(target=False))
    tgt_only = bracket_order(_stk(stop_type="none", target_price="195.00"))
    bare = bracket_order(_stk(stop_type="none", target=False))
    assert full["orderStrategyType"] == "TRIGGER" and full["childOrderStrategies"][0]["orderStrategyType"] == "OCO"
    (s,) = stop_only["childOrderStrategies"]
    assert stop_only["orderStrategyType"] == "TRIGGER" and s["orderType"] == "STOP" and s["stopPrice"] == "186.50"
    assert s["orderLegCollection"][0]["instruction"] == "SELL" and s["orderLegCollection"][0]["quantity"] == 100
    (t,) = tgt_only["childOrderStrategies"]
    assert t["orderType"] == "LIMIT" and t["price"] == "195.00" and t["orderLegCollection"][0]["instruction"] == "SELL"
    assert bare["orderStrategyType"] == "SINGLE" and "childOrderStrategies" not in bare
    assert bare["orderType"] == "LIMIT" and bare["price"] == "188.65"
    mkt = bracket_order(_stk(stop_type="none", target=False, entry="market"))
    assert mkt["orderType"] == "MARKET" and "price" not in mkt


def test_trail_without_a_stop_price_in_quantity_mode():
    p = _stk(stop="", stop_type="trail", trail_unit="$", trail_value=1.5, target=False)
    assert p.ok, p.errors
    assert p.trail["offset"] == Decimal("1.50") and p.per_share == 0
    (s,) = bracket_order(p)["childOrderStrategies"]
    assert s["orderType"] == "TRAILING_STOP"
    assert "type a stop price" in _stk(stop="", stop_type="trail", trail_unit="R").errors[0]
    assert "type a stop price" in _stk(stop="", stop_type="autobe").errors[0]


def test_option_quantity_without_a_stop():
    p = _opt(size_mode="qty", qty=5, stop_type="none", target=False)
    assert p.ok, p.errors
    order = bracket_order(p)
    assert order["orderStrategyType"] == "SINGLE" and order["orderLegCollection"][0]["instruction"] == "BUY_TO_OPEN"
    short = _opt(size_mode="qty", qty=2, side="SHORT", stop_type="none", target=False)
    assert any("no limit on the loss" in w for w in short.warnings)
