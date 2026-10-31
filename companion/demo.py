"""Synthetic dashboard state for screenshots and UI development: `python -m companion --demo`.

Every number here is invented. Demo mode never connects to Schwab, keeps its own throwaway
database (data/demo.db) and refuses every order action. A normal start never loads it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _iso(t: datetime) -> str:
    return t.isoformat()


def _pos(sym, side, qty, avg, last, stop, target, opened, held="INTRADAY", r=None, bid=None, ask=None,
         protection="FULL", stop_qty=None, name_display=None, asset="EQUITY", underlying=None, mult=1.0):
    d = 1 if side == "LONG" else -1
    open_pnl = round((last - avg) * qty * d * mult, 2)
    at_stop = None if stop is None else round((stop - avg) * qty * d * mult, 2)
    bid = bid or round(last - 0.01, 2)
    ask = ask or round(last + 0.01, 2)
    return {
        "symbol": sym, "display": name_display or sym, "asset_type": asset, "underlying": underlying or sym,
        "side": side, "qty": float(qty), "avg_price": avg, "last": last,
        "quote": {"last": last, "bid": bid, "ask": ask, "mark": round((bid + ask) / 2, 2), "change_pct": 1.12},
        "multiplier": mult, "market_value": round(last * qty * d * mult, 2), "open_pnl": open_pnl,
        "open_pnl_pct": round(open_pnl / (avg * qty * mult) * 100, 2), "day_pnl": open_pnl if held == "INTRADAY" else -120.0,
        "held": held, "stop_price": stop, "target_price": target, "stop_qty": float(stop_qty if stop_qty is not None else (qty if stop else 0)),
        "protection": protection, "pnl_at_stop": at_stop, "trail": None,
        "targets_all": ([{"price": target, "qty": float(qty)}] if target else []),
        "ledger": "MATCH", "opened": _iso(opened) if opened else None, "r": r,
    }


def _order(oid, sym, role, instr, otype, qty, price=None, stop=None, last=None, group=None, gtype="OCO",
           duration="DAY", cancellable=False, status="WORKING", effect="CLOSING", asset="EQUITY", display=None,
           underlying=None):
    ref = stop if stop is not None else price
    return {
        "order_id": oid, "leg_id": 1, "legs": 1, "group_id": group or oid, "group_type": gtype, "status": status,
        "live": True, "symbol": sym, "display": display or sym, "underlying": underlying or sym, "asset_type": asset,
        "instruction": instr, "effect": effect, "order_type": otype, "role": role, "qty": float(qty), "filled": 0.0,
        "price": price, "stop": stop, "duration": duration, "entered": "", "tag": None, "last": last,
        "distance": round(ref - last, 2) if (ref and last) else None,
        "distance_pct": round((ref - last) / last * 100, 2) if (ref and last) else None,
        "transitional": False, "cancellable": cancellable,
    }


_STARTED = datetime.now(timezone.utc)      # fixed open times: positions do not "reopen" every poll
OPT, OPT_DISPLAY = "AAPL  261120C00230000", "AAPL 20 Nov 26 230C"


def demo_state(now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    t = _STARTED
    ok = _iso(now - timedelta(seconds=1))
    fresh = {k: {"ok_at": ok, "error": None, "interval": i}
             for k, i in (("account", 5), ("orders_today", 5), ("orders_sweep", 30), ("transactions", 10), ("quotes", 3))}
    positions = [
        _pos("NVDA", "LONG", 180, 187.42, 188.65, 187.42, 191.86, t - timedelta(minutes=48),
             r={"side": "LONG", "entry": 187.42, "stop": 185.20, "rps": 2.22, "live_r": 0.55}),
        _pos("AMD", "SHORT", 90, 162.30, 161.05, 164.10, 158.70, t - timedelta(minutes=21),
             r={"side": "SHORT", "entry": 162.30, "stop": 164.10, "rps": 1.80, "live_r": 0.69}),
        _pos(OPT, "LONG", 4, 0.52, 0.73, 0.30, 0.95, t - timedelta(minutes=6), bid=0.72, ask=0.74,
             r={"side": "LONG", "entry": 0.52, "stop": 0.30, "rps": 0.22, "live_r": 0.95},
             name_display=OPT_DISPLAY, asset="OPTION", underlying="AAPL", mult=100.0),
        _pos("PLTR", "LONG", 400, 41.60, 41.38, 41.10, None, t - timedelta(minutes=9),
             protection="PARTIAL", stop_qty=200),
        _pos("MSFT", "LONG", 50, 402.10, 431.50, None, 445.00, None, held="CARRIED", protection="NONE"),
    ]
    orders = [
        _order(9001, "NVDA", "STOP", "SELL", "STOP", 180, stop=187.42, last=188.65, group=9000),
        _order(9002, "NVDA", "TARGET", "SELL", "LIMIT", 180, price=191.86, last=188.65, group=9000),
        _order(9011, "AMD", "STOP", "BUY_TO_COVER", "STOP", 90, stop=164.10, last=161.05, group=9010),
        _order(9012, "AMD", "TARGET", "BUY_TO_COVER", "LIMIT", 90, price=158.70, last=161.05, group=9010),
        _order(9051, OPT, "STOP", "SELL_TO_CLOSE", "STOP", 4, stop=0.30, last=0.73, group=9050,
               asset="OPTION", display=OPT_DISPLAY, underlying="AAPL"),
        _order(9052, OPT, "TARGET", "SELL_TO_CLOSE", "LIMIT", 4, price=0.95, last=0.73, group=9050,
               asset="OPTION", display=OPT_DISPLAY, underlying="AAPL"),
        _order(9021, "PLTR", "STOP", "SELL", "STOP", 200, stop=41.10, last=41.38, gtype="SINGLE"),
        _order(9031, "AAPL", "ENTRY", "BUY", "LIMIT", 120, price=229.40, last=229.55, gtype="TRIGGER",
               cancellable=True, effect="OPENING"),
        _order(9041, "MSFT", "TARGET", "SELL", "LIMIT", 50, price=445.00, last=431.50, gtype="SINGLE", duration="GOOD_TILL_CANCEL"),
    ]
    t0 = now - timedelta(hours=3)
    closed = [
        {"symbol": s, "display": s, "asset_type": "EQUITY", "side": side, "status": "MATCHED",
         "opened": _iso(t0 + timedelta(minutes=m)), "closed": _iso(t0 + timedelta(minutes=m + h)),
         "max_qty": float(q), "exit_qty": float(q), "avg_entry": a, "avg_exit": b,
         "gross": g, "fees": -0.6, "net": round(g - 0.6, 2), "unresolved_qty": 0.0, "multiplier": 1.0,
         "hold_seconds": h * 60}
        for s, side, q, a, b, g, m, h in (
            ("TSLA", "LONG", 60, 251.20, 254.85, 219.0, 5, 22), ("META", "SHORT", 40, 588.40, 590.10, -68.0, 40, 11),
            ("AAPL", "LONG", 150, 228.10, 229.30, 180.0, 70, 35), ("AMZN", "LONG", 100, 201.50, 200.95, -55.0, 120, 8))]
    real = round(sum(c["net"] for c in closed), 2)
    day_open = round(sum(p["open_pnl"] for p in positions if p["held"] == "INTRADAY"), 2)
    sec = {"net": real, "estimated": 0.0, "estimated_closes": 0, "unresolved_closes": 0, "complete": True,
           "lots_complete": True, "fills_pending": 0.0}
    fut = {"net": 0.0, "gross": 0.0, "fees": 0.0, "estimated": 0.0, "estimated_closes": 0, "unresolved_closes": 0,
           "complete": True, "lots_complete": True}
    brackets = [
        {"created": _iso(t - timedelta(minutes=48)), "side": "LONG", "symbol": "NVDA", "limit_price": "187.42", "qty": 180,
         "entry_status": "FILLED", "status": "ACCEPTED", "stop": "185.20", "target": "191.86", "protection": "PROTECTED",
         "stop_qty": 180, "exposure": 180, "target_qty": 180, "ticket_id": "demo-1", "order_id": 9000, "detail": None},
        {"created": _iso(t - timedelta(minutes=21)), "side": "SHORT", "symbol": "AMD", "limit_price": "162.30", "qty": 90,
         "entry_status": "FILLED", "status": "ACCEPTED", "stop": "164.10", "target": "158.70", "protection": "PROTECTED",
         "stop_qty": 90, "exposure": 90, "target_qty": 90, "ticket_id": "demo-2", "order_id": 9010, "detail": None},
        {"created": _iso(t - timedelta(minutes=2)), "side": "LONG", "symbol": "AAPL", "limit_price": "229.40", "qty": 120,
         "entry_status": "WORKING", "status": "ACCEPTED", "stop": "227.60", "target": "233.00", "protection": "WAITING_FILL",
         "stop_qty": 0, "exposure": 0, "target_qty": None, "ticket_id": "demo-3", "order_id": 9031, "detail": None},
    ]
    log = {"NVDA": [{"t": _iso(t - timedelta(minutes=48)), "text": "Ticket LONG 180 @ 187.42, stop 185.20, target 191.86: ACCEPTED", "kind": "ticket"},
                    {"t": _iso(t - timedelta(minutes=48)), "text": "Fill bought 180 @ 187.42", "kind": "fill"},
                    {"t": _iso(t - timedelta(minutes=12)), "text": "move stop: DONE Stop moved to 187.42 on 180 of 180 shares, OCO kept", "kind": "action"}]}
    return {
        "generated": _iso(now), "today": now.date().isoformat(), "futures_session": now.date().isoformat(),
        "auth_hours_left": 130.0, "freshness": fresh, "history_complete": True,
        "summary": {"realized": {"securities": sec, "futures": fut, "net": real, "complete": True},
                    "open_pnl": {"intraday": day_open, "mixed": 0.0, "carried": positions[-1]["open_pnl"], "positions": len(positions)},
                    "trades_closed": len(closed), "wins": 2, "losses": 2, "unknown": 0, "fees_today": -2.4},
        "positions": positions, "orders": orders, "closed": closed,
        "futures": {"observed_net_today": {}, "note": "Futures positions and orders are not available from Schwab's API."},
        "balances": {"liquidationValue": 100000.0}, "status": "live", "last_rebuild": _iso(now),
        "market_hours": {"today": None, "next": {"date": now.date().isoformat(), "open": _iso(now - timedelta(hours=4)),
                                                 "close": _iso(now + timedelta(hours=1, minutes=12))}},
        "brackets": brackets, "action_log": log,
    }


# ---- invented quotes and option chain, so the ticket can be tried in demo (never sent) ----------

_DEMO_LAST = {"AAPL": 229.84, "NVDA": 188.65, "AMD": 161.05, "PLTR": 41.38, "MSFT": 431.50, "SPY": 598.20}
_DEMO_NAMES = {"AAPL": "Apple Inc", "NVDA": "NVIDIA Corp", "AMD": "Advanced Micro Devices", "PLTR": "Palantir",
               "MSFT": "Microsoft Corp", "SPY": "SPDR S&P 500 ETF"}


def _occ(root: str, exp: str, pc: str, strike: float) -> str:
    return f"{root:<6}{exp[2:4]}{exp[5:7]}{exp[8:10]}{pc}{int(round(strike * 1000)):08d}"


def _premium(spot: float, strike: float, pc: str, dte: int) -> tuple[float, float, float, float]:
    """(bid, ask, mark, delta) of an invented contract: intrinsic + a time value that fades away
    from the money. Plausible-looking, not a pricing model."""
    import math
    intrinsic = max(0.0, spot - strike) if pc == "C" else max(0.0, strike - spot)
    width = 2.5 + 0.6 * dte
    tv = (0.9 + 0.35 * dte) * math.exp(-abs(strike - spot) / width)
    mark = round(intrinsic + tv, 2)
    half = max(0.01, round(mark * 0.015, 2))
    bid, ask = max(0.01, round(mark - half, 2)), round(mark + half, 2)
    x = (spot - strike) / width
    d = 1 / (1 + math.exp(-1.7 * x))
    return bid, ask, round((bid + ask) / 2, 3), round(d if pc == "C" else d - 1, 3)


def demo_expirations(underlying: str) -> list[dict]:
    from datetime import date
    und = (underlying or "").upper().strip()
    if und not in _DEMO_LAST:
        raise ValueError(f"demo: no options for {und or '?'} (try AAPL)")
    today = date.today()
    out, d = [], today
    while len(out) < 7:
        if d.weekday() in (0, 2, 4):
            out.append({"date": d.isoformat(), "dte": (d - today).days, "type": "W", "standard": True})
        d += timedelta(days=1)
    for months in (1, 2):
        m = (today.month - 1 + months) % 12 + 1
        y = today.year + (today.month - 1 + months) // 12
        first = date(y, m, 1)
        third_fri = first + timedelta(days=(4 - first.weekday()) % 7 + 14)
        out.append({"date": third_fri.isoformat(), "dte": (third_fri - today).days, "type": "S", "standard": True})
    return out


def demo_chain(underlying: str, expiration: str, put_call: str, strikes: int = 16) -> dict:
    from datetime import date
    und = (underlying or "").upper().strip()
    spot = _DEMO_LAST.get(und)
    if spot is None:
        raise ValueError(f"demo: no options for {und or '?'} (try AAPL)")
    pc = "C" if (put_call or "").upper() == "CALL" else "P"
    dte = max(0, (date.fromisoformat(expiration) - date.today()).days)
    step = 2.5 if spot < 300 else 5.0
    atm = round(spot / step) * step
    rows = []
    for i in range(-(strikes // 2), strikes // 2):
        k = atm + i * step
        bid, ask, mark, delta = _premium(spot, k, pc, dte)
        sym = _occ(und, expiration, pc, k)
        rows.append({"symbol": sym, "description": f"{und} {expiration} {k:g} {pc}", "strike": k, "bid": bid,
                     "ask": ask, "mark": mark, "last": mark, "delta": delta, "iv": 28.0,
                     "oi": int(9000 * (0.3 + abs(delta) * (1 - abs(delta)) * 3)), "volume": int(4000 * abs(delta)),
                     "spread_pct": round((ask - bid) / mark * 100, 1) if mark else None,
                     "itm": (spot > k) if pc == "C" else (spot < k), "dte": dte, "penny": True, "standard": True})
    return {"underlying": und, "expiration": expiration, "put_call": put_call.upper(), "underlying_price": spot,
            "underlying_name": _DEMO_NAMES.get(und), "rows": rows}


def demo_quote(symbol: str, max_age: float = 1.0) -> dict:
    """A stock or option quote in the shape Trader.quote returns."""
    from datetime import date
    from .bracket import ticks_for
    sym = (symbol or "").upper().strip()
    if len(sym) > 15:                                   # OCC option symbol
        root, exp = sym[:6].strip(), f"20{sym[6:8]}-{sym[8:10]}-{sym[10:12]}"
        pc, strike = sym[12], int(sym[13:]) / 1000
        spot = _DEMO_LAST.get(root)
        if spot is None:
            return {"symbol": sym, "found": False}
        dte = max(0, (date.fromisoformat(exp) - date.today()).days)
        bid, ask, mark, _ = _premium(spot, strike, pc, dte)
        return {"symbol": sym, "found": True, "bid": bid, "ask": ask, "last": mark, "mark": mark, "change_pct": 4.2,
                "name": f"{root} {exp} {strike:g} {'Call' if pc == 'C' else 'Put'}", "exchange": "OPR",
                "asset_type": "OPTION", "underlying": root, "multiplier": 100.0, "is_penny": True, "realtime": True,
                "deliverables": f"100 {root}", "put_call": pc, "expiration": exp, "strike": strike,
                "ticks": ticks_for("OPTION", root, True)}
    last = _DEMO_LAST.get(sym)
    if last is None:
        return {"symbol": sym, "found": False}
    return {"symbol": sym, "found": True, "bid": round(last - 0.03, 2), "ask": round(last + 0.03, 2), "last": last,
            "mark": last, "change_pct": 1.12, "name": _DEMO_NAMES.get(sym), "exchange": "NASDAQ",
            "asset_type": "EQUITY", "underlying": None, "multiplier": None, "is_penny": None, "realtime": True,
            "ticks": "EQUITY"}
