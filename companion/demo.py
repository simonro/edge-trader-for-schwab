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
           duration="DAY", cancellable=False, status="WORKING", effect="CLOSING"):
    ref = stop if stop is not None else price
    return {
        "order_id": oid, "leg_id": 1, "legs": 1, "group_id": group or oid, "group_type": gtype, "status": status,
        "live": True, "symbol": sym, "display": sym, "underlying": sym, "asset_type": "EQUITY",
        "instruction": instr, "effect": effect, "order_type": otype, "role": role, "qty": float(qty), "filled": 0.0,
        "price": price, "stop": stop, "duration": duration, "entered": "", "tag": None, "last": last,
        "distance": round(ref - last, 2) if (ref and last) else None,
        "distance_pct": round((ref - last) / last * 100, 2) if (ref and last) else None,
        "transitional": False, "cancellable": cancellable,
    }


_STARTED = datetime.now(timezone.utc)      # fixed open times: positions do not "reopen" every poll


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
        _pos("PLTR", "LONG", 400, 41.60, 41.38, 41.10, None, t - timedelta(minutes=9),
             protection="PARTIAL", stop_qty=200),
        _pos("MSFT", "LONG", 50, 402.10, 431.50, None, 445.00, None, held="CARRIED", protection="NONE"),
    ]
    orders = [
        _order(9001, "NVDA", "STOP", "SELL", "STOP", 180, stop=187.42, last=188.65, group=9000),
        _order(9002, "NVDA", "TARGET", "SELL", "LIMIT", 180, price=191.86, last=188.65, group=9000),
        _order(9011, "AMD", "STOP", "BUY_TO_COVER", "STOP", 90, stop=164.10, last=161.05, group=9010),
        _order(9012, "AMD", "TARGET", "BUY_TO_COVER", "LIMIT", 90, price=158.70, last=161.05, group=9010),
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
