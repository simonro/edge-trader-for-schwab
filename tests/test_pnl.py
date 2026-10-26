from datetime import date

from companion.pnl import Opening, build_book, fills_from_transactions, opening_inventory

_id = iter(range(1, 10_000))


def txn(time, symbol, qty, price, effect, asset="EQUITY", mult=1.0, fee=0.0):
    return {
        "activityId": next(_id), "orderId": 1, "type": "TRADE", "status": "VALID",
        "time": time,
        "transferItems": [
            {"instrument": {"assetType": "CURRENCY", "symbol": "CURRENCY_USD"},
             "amount": -fee, "cost": -fee, "feeType": "COMMISSION"},
            {"instrument": {"assetType": asset, "symbol": symbol},
             "amount": qty, "price": price, "cost": -qty * price * mult,
             "positionEffect": effect},
        ],
    }


D = date(2026, 10, 20)


def book(*txns):
    fills = fills_from_transactions(list(txns))
    return build_book(fills), fills


def test_long_round_trip_with_partials():
    b, fills = book(
        txn("2026-10-20T14:00:00+0000", "TSX", 100, 10.0, "OPENING"),
        txn("2026-10-20T14:01:00+0000", "TSX", 100, 11.0, "OPENING"),
        txn("2026-10-20T14:05:00+0000", "TSX", -150, 12.0, "CLOSING", fee=1.0),
        txn("2026-10-20T14:06:00+0000", "TSX", -50, 9.0, "CLOSING", fee=1.0),
    )
    (t,) = b.trades
    # FIFO: 100@10 +200, 50@11 +50, 50@11 -100
    assert t.realized == 150.0
    assert t.closed is not None and t.max_qty == 200
    r = b.realized_on(D, fills)
    assert (r["gross"], r["fees"], r["net"], r["lots_complete"]) == (150.0, -2.0, 148.0, True)


def test_short_and_futures_multiplier():
    b, _ = book(
        txn("2026-10-20T13:35:00+0000", "/ESZ26:XCME", -2, 7722.75, "OPENING", "FUTURE", 50),
        txn("2026-10-20T13:40:00+0000", "/ESZ26:XCME", 1, 7721.75, "CLOSING", "FUTURE", 50),
        txn("2026-10-20T13:41:00+0000", "/ESZ26:XCME", 1, 7722.75, "CLOSING", "FUTURE", 50),
    )
    (t,) = b.trades
    assert t.symbol == "/ESZ26" and t.side == "SHORT" and t.multiplier == 50
    assert t.realized == 50.0


def test_option_multiplier_100():
    b, _ = book(
        txn("2026-10-20T14:00:00+0000", "SPY   261020C00600000", 2, 1.50, "OPENING", "OPTION", 100),
        txn("2026-10-20T14:10:00+0000", "SPY   261020C00600000", -2, 2.00, "CLOSING", "OPTION", 100),
    )
    assert b.trades[0].realized == 100.0


def test_open_position_stays_open():
    b, _ = book(
        txn("2026-10-20T14:00:00+0000", "MXR", 100, 10.0, "OPENING"),
        txn("2026-10-20T14:05:00+0000", "MXR", -40, 11.0, "CLOSING"),
    )
    (t,) = b.open_trades()
    assert t.open_qty == 60 and t.realized == 40.0
    assert b.open_lots["MXR"] == [[60.0, 10.0, True]]


def test_flip_through_zero_starts_new_trade():
    b, _ = book(
        txn("2026-10-20T14:00:00+0000", "TSLX", 100, 10.0, None),
        txn("2026-10-20T14:05:00+0000", "TSLX", -150, 12.0, None),
    )
    long_, short = b.trades
    assert long_.side == "LONG" and long_.realized == 200.0 and long_.closed
    assert short.side == "SHORT" and short.open_qty == 50


def test_close_with_no_inventory_is_unresolved_not_zero():
    b, fills = book(txn("2026-10-20T14:00:00+0000", "AKMX", -200, 124.0, "CLOSING"))
    (t,) = b.trades
    assert t.status == "UNRESOLVED" and t.unresolved_qty == 200 and t.net is None
    r = b.realized_on(D, fills)
    assert r["gross"] == 0.0 and r["unresolved_closes"] == 1 and not r["lots_complete"]


def test_opening_inventory_with_basis_is_estimated():
    fills = fills_from_transactions([txn("2026-10-20T14:00:00+0000", "AKMX", -200, 124.0, "CLOSING")])
    op = opening_inventory(fills, {"AKMX": (200.0, 121.0)}, D)
    b = build_book(fills, op)
    (t,) = b.trades
    assert t.status == "ESTIMATED" and t.realized == 600.0 and t.closed and t.opened is None
    r = b.realized_on(D, fills)
    assert r["matched"] == 0 and r["estimated"] == 600.0 and r["estimated_closes"] == 1


def test_fifo_consumes_older_inventory_before_new_lots():
    # 200 held from before the history, buy 50 today, sell 100: FIFO sells old shares.
    fills = fills_from_transactions([
        txn("2026-10-20T14:00:00+0000", "GGLX", 50, 100.0, "OPENING"),
        txn("2026-10-20T14:05:00+0000", "GGLX", -100, 101.0, "CLOSING"),
    ])
    b = build_book(fills, opening_inventory(fills, {"GGLX": (200.0, None)}, D))
    (t,) = b.trades
    assert t.closed is None and t.open_qty == 150 and t.status == "UNRESOLVED"
    assert t.unresolved_qty == 100 and t.realized == 0.0
    assert [lot[0] for lot in b.open_lots["GGLX"]] == [100.0, 50.0]


def test_split_closing_transactions_do_not_create_extra_trades():
    fills = fills_from_transactions([
        txn("2026-10-20T14:05:00+0000", "MXR", -60, 11.0, "CLOSING"),
        txn("2026-10-20T14:05:00+0000", "MXR", -40, 11.0, "CLOSING"),
    ])
    b = build_book(fills, opening_inventory(fills, {"MXR": (100.0, 10.0)}, D))
    assert len(b.trades) == 1 and b.trades[0].realized == 100.0 and b.trades[0].closed


def test_same_second_close_then_reverse():
    # Long 1 @100 from yesterday; in one second sell 1 @101 (close) and 1 @102 (open short).
    fills = fills_from_transactions([
        txn("2026-10-19T15:00:00+0000", "X", 1, 100.0, "OPENING"),
        txn("2026-10-20T14:00:00+0000", "X", -1, 102.0, "OPENING"),
        txn("2026-10-20T14:00:00+0000", "X", -1, 101.0, "CLOSING"),
    ])
    b = build_book(fills)
    long_, short = b.trades
    assert long_.realized == 1.0 and long_.closed
    assert short.side == "SHORT" and short.open_qty == 1 and short.avg_entry == 102.0


def test_same_second_open_then_close_when_flat():
    b, _ = book(
        txn("2026-10-20T14:00:00+0000", "Y", -1, 101.0, "CLOSING"),
        txn("2026-10-20T14:00:00+0000", "Y", 1, 100.0, "OPENING"),
    )
    (t,) = b.trades
    assert t.realized == 1.0 and t.closed and t.status == "MATCHED"


def test_replay_is_deterministic():
    raw = [txn("2026-10-20T14:00:00+0000", "Z", 10, 5.0, "OPENING"),
           txn("2026-10-20T14:01:00+0000", "Z", -10, 6.0, "CLOSING")]
    a = build_book(fills_from_transactions(raw + raw))   # duplicates dedupe by id
    assert len(a.trades) == 1 and a.trades[0].realized == 10.0


def test_futures_evening_fill_belongs_to_next_session():
    b, fills = book(
        txn("2026-10-19T22:30:00+0000", "/MNQZ26:XCME", 1, 100.0, "OPENING", "FUTURE", 2),
        txn("2026-10-19T23:00:00+0000", "/MNQZ26:XCME", -1, 110.0, "CLOSING", "FUTURE", 2),
    )
    assert b.trades[0].close_day == D
    assert b.realized_on(D, fills)["gross"] == 20.0
