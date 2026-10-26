"""Build the dashboard state (one JSON document) from cached broker data.

Pure: takes the latest raw payloads, returns a dict. Nothing here calls Schwab.
Rule from the design review: an incomplete view must never look complete, so
every number carries its coverage (complete / estimated / unknown).
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .orders import PendingOrder, pending_orders, stop_coverage
from .pnl import (Book, Fill, build_book, fills_from_transactions, opening_inventory,
                  session_day)

_OCC = re.compile(r"^(?P<root>[A-Z.]+)\s*(?P<y>\d\d)(?P<m>\d\d)(?P<d>\d\d)(?P<cp>[CP])(?P<strike>\d{8})$")
_MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def pretty_symbol(symbol: str, asset_type: str) -> str:
    if asset_type == "OPTION":
        m = _OCC.match(symbol)
        if m:
            strike = int(m["strike"]) / 1000
            strike_s = f"{strike:g}"
            return f"{m['root']} {int(m['d'])} {_MONTHS[int(m['m']) - 1]} {m['y']} {strike_s}{m['cp']}"
    return symbol


def _f(x) -> float:
    try:
        return float(x or 0)
    except (TypeError, ValueError):
        return 0.0


def _mark(sym: str, quotes: dict) -> dict | None:
    q = (quotes.get(sym) or {}).get("quote") or {}
    if not q:
        return None
    return {"last": q.get("lastPrice"), "bid": q.get("bidPrice"), "ask": q.get("askPrice"),
            "mark": q.get("mark"), "change_pct": q.get("netPercentChange")}


def _order_row(p: PendingOrder, quotes: dict) -> dict:
    m = _mark(p.symbol, quotes)
    trigger = p.stop if p.stop is not None else p.price
    ref = (m or {}).get("last") or (m or {}).get("mark")
    dist = dist_pct = None
    if trigger is not None and ref and p.legs == 1:
        dist = trigger - ref
        dist_pct = dist / ref * 100
    return {
        "order_id": p.order_id, "leg_id": p.leg_id, "legs": p.legs,
        "group_id": p.group_id, "group_type": p.group_type,
        "status": p.status, "live": p.live, "symbol": p.symbol,
        "display": pretty_symbol(p.symbol, p.asset_type), "underlying": p.underlying,
        "asset_type": p.asset_type, "instruction": p.instruction, "effect": p.effect,
        "order_type": p.order_type, "role": p.role, "qty": p.qty, "filled": p.filled,
        "price": p.price, "stop": p.stop, "duration": p.duration, "entered": p.entered,
        "tag": p.tag, "last": ref, "distance": dist, "distance_pct": dist_pct,
        "transitional": p.status in ("PENDING_CANCEL", "PENDING_REPLACE"),
        # A top-level entry with nothing filled can be cancelled from the dashboard
        # (the server re-checks with Schwab before cancelling).
        "cancellable": p.role == "ENTRY" and p.parent_id is None and p.filled == 0 and p.live,
    }


def private_view(snap: dict) -> dict:
    """The dashboard state without carried positions, for screen recordings (`--private`).

    Display only: order actions read the poller directly. Positions held from before today
    (CARRIED, and MIXED ones carried and traded today) are dropped with their working orders,
    action log and tickets; the carried / mixed open P&L and the account value are blanked.
    """
    positions = snap.get("positions") or []
    hidden = {p["symbol"] for p in positions if p.get("held") != "INTRADAY"}
    out = {**snap, "private": True,
           "positions": [p for p in positions if p["symbol"] not in hidden],
           "orders": [o for o in snap.get("orders") or [] if o.get("symbol") not in hidden],
           "brackets": [b for b in snap.get("brackets") or [] if b.get("symbol") not in hidden],
           "action_log": {s: v for s, v in (snap.get("action_log") or {}).items() if s not in hidden},
           "balances": {}}
    summary = snap.get("summary")
    if summary and summary.get("open_pnl"):
        o = summary["open_pnl"]
        out["summary"] = {**summary, "open_pnl": {**o, "mixed": 0.0, "carried": None,
                                                   "positions": len(out["positions"])}}
    return out


def _held(p: dict, side: str, qty: float) -> str:
    prev = _f(p.get("previousSessionLongQuantity") if side == "LONG" else p.get("previousSessionShortQuantity"))
    if prev < 1e-9:
        return "INTRADAY"
    if abs(prev - abs(qty)) < 1e-9 and _f(p.get("currentDayCost")) == 0:
        return "CARRIED"
    return "MIXED"   # carried and traded today: not purely either


def _position_rows(account: dict, pend: list[PendingOrder], quotes: dict, book: Book,
                   orders_fresh: bool) -> list[dict]:
    rows = []
    for p in account.get("positions") or []:
        ins = p.get("instrument") or {}
        sym = ins.get("symbol", "?")
        at = ins.get("assetType", "?")
        qty = _f(p.get("longQuantity")) - _f(p.get("shortQuantity"))
        if abs(qty) < 1e-9:
            continue
        side = "LONG" if qty > 0 else "SHORT"
        size = abs(qty)
        mult = 100.0 if at == "OPTION" else 1.0
        avg = _f(p.get("averagePrice"))
        open_pnl = _f(p.get("longOpenProfitLoss") if side == "LONG" else p.get("shortOpenProfitLoss"))
        m = _mark(sym, quotes)
        last = (m or {}).get("last") or (abs(_f(p.get("marketValue"))) / (size * mult))
        direction = 1 if side == "LONG" else -1

        covered, alloc = stop_coverage(pend, sym, side, size)
        if not orders_fresh:
            protection = "UNKNOWN"          # order data stale: do not claim anything
        elif covered < 1e-9:
            protection = "NONE"
        elif covered + 1e-9 < size:
            protection = "PARTIAL"
        else:
            protection = "FULL"
        at_stop = None
        if alloc and avg and all(px is not None for px, _ in alloc):
            # If the allocated stops trigger at their stop prices (fills are not guaranteed).
            at_stop = sum((px - avg) * q * direction * mult for px, q in alloc)
        stop_prices = [px for px, _ in alloc if px is not None]
        targets = [o for o in pend if o.symbol == sym and o.role == "TARGET" and o.live
                   and o.legs == 1 and o.exits(side) and o.price is not None]

        trailing = next((o for o in pend if o.symbol == sym and o.role == "STOP" and o.live and o.exits(side)
                         and o.order_type.startswith("TRAILING")), None)
        n_stops = len({(o.oco_id or o.order_id) for o in pend if o.symbol == sym and o.role == "STOP" and o.live
                       and o.exits(side)})
        trail = ({"order_id": trailing.order_id, "offset": trailing.trail_offset, "link": trailing.trail_link,
                  "basis": trailing.trail_basis, "schwab_stop": trailing.stop, "entered": trailing.entered,
                  # Only a single trailing stop covering every share may stand for the position's stop.
                  "whole": n_stops == 1 and trailing.qty + 1e-9 >= size} if trailing else None)
        ledger_qty = sum(lot[0] for lot in book.open_lots.get(sym, []))
        open_trade = next((t for t in book.open_trades() if t.symbol == sym), None)
        rows.append({
            "symbol": sym, "display": pretty_symbol(sym, at), "asset_type": at,
            "underlying": ins.get("underlyingSymbol") or sym, "side": side, "qty": size,
            "avg_price": avg, "last": last, "quote": m, "multiplier": mult,
            "market_value": _f(p.get("marketValue")), "open_pnl": open_pnl,
            "open_pnl_pct": (open_pnl / (avg * size * mult) * 100) if avg else None,
            "day_pnl": _f(p.get("currentDayProfitLoss")), "held": _held(p, side, qty),
            "stop_price": ((min if side == "LONG" else max)(stop_prices) if stop_prices else None),
            "target_price": ((max if side == "LONG" else min)(o.price for o in targets) if targets else None),
            "stop_qty": covered, "protection": protection, "pnl_at_stop": at_stop,
            "trail": trail,     # Schwab-held trailing stop; the poller adds its estimated level
            # Every live target (a two-target bracket has two), nearest first.
            "targets_all": sorted(({"price": o.price, "qty": o.qty} for o in targets),
                                  key=lambda t: t["price"], reverse=(side == "SHORT")),
            # Our fill ledger vs Schwab's quantity: a mismatch means fills are missing or late.
            "ledger": "MATCH" if abs(ledger_qty - qty) < 1e-9 else "MISMATCH",
            # When the current flat-to-flat position started (first fill), for R ownership.
            "opened": open_trade.opened.isoformat() if open_trade and open_trade.opened else None,
        })
    # Stable order: intraday, mixed, carried, then by symbol (rows must not jump as P&L moves).
    order = {"INTRADAY": 0, "MIXED": 1, "CARRIED": 2}
    return sorted(rows, key=lambda r: (order[r["held"]], r["underlying"], r["symbol"]))


def _trade_row(t) -> dict:
    return {
        "symbol": t.symbol, "display": pretty_symbol(t.symbol, t.asset_type),
        "asset_type": t.asset_type, "side": t.side, "status": t.status,
        "opened": t.opened.isoformat() if t.opened else None,
        "closed": t.closed.isoformat() if t.closed else None,
        "max_qty": t.max_qty, "exit_qty": t.exit_qty, "avg_entry": t.avg_entry,
        "avg_exit": t.avg_exit,
        "gross": None if t.status == "UNRESOLVED" else t.realized,
        "fees": t.fees, "net": t.net,
        "unresolved_qty": t.unresolved_qty, "multiplier": t.multiplier,
        "hold_seconds": (t.closed - t.opened).total_seconds() if t.closed and t.opened else None,
    }


def _filled_in_orders(orders: list[dict], day) -> float:
    """Shares and contracts executed today across every order node (children included), dated by
    each execution's own time."""
    from .pnl import ET as _ET
    total = 0.0

    def walk(o):
        nonlocal total
        legs = o.get("orderLegCollection") or []
        if legs and all((l.get("instrument") or {}).get("assetType") in ("EQUITY", "OPTION") for l in legs):
            # Each execution carries its own time: an old GTC order that fills today counts today.
            for act in o.get("orderActivityCollection") or []:
                if act.get("executionType") != "FILL":
                    continue
                for e in act.get("executionLegs") or []:
                    try:
                        d = datetime.strptime(e.get("time") or "", "%Y-%m-%dT%H:%M:%S%z").astimezone(_ET).date()
                    except ValueError:
                        continue
                    if d == day:
                        total += _f(e.get("quantity"))
        for ch in o.get("childOrderStrategies") or []:
            walk(ch)

    seen = set()
    for o in orders:
        if o.get("orderId") in seen:
            continue
        seen.add(o.get("orderId"))
        walk(o)
    return total


def build_state(*, account: dict | None, orders: list[dict], txns: list[dict], quotes: dict,
                prev_session: dict[str, tuple[float, float | None]], freshness: dict,
                history_complete: bool, orders_fresh: bool, auth_hours_left: float | None,
                known_terminal: set[int] | None = None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    eq_today = session_day(now, "EQUITY")
    fut_today = session_day(now, "FUTURE")      # rolls at 18:00 ET
    fills: list[Fill] = fills_from_transactions(txns)
    book = build_book(fills, opening_inventory(fills, prev_session, eq_today))
    pend = pending_orders(orders, known_terminal)
    acct = (account or {}).get("securitiesAccount") or {}

    positions = _position_rows(acct, pend, quotes, book, orders_fresh) if acct else []
    closed_today = sorted(book.closed_on(eq_today, {"EQUITY", "OPTION"})
                          + book.closed_on(fut_today, {"FUTURE"}),
                          key=lambda t: t.closed, reverse=True)
    sec = book.realized_on(eq_today, fills, {"EQUITY", "OPTION"})
    fut = book.realized_on(fut_today, fills, {"FUTURE"})
    for r in (sec, fut):
        r["complete"] = r["lots_complete"] and history_complete
    # Stock and option shares Schwab's orders report as filled today versus the fills already in
    # /transactions: more in the orders means fills are still on their way.
    order_filled = _filled_in_orders(orders, eq_today)
    tx_filled = sum(abs(f.qty) for f in fills if f.asset_type in ("EQUITY", "OPTION") and f.day == eq_today)
    sec["fills_pending"] = max(0.0, order_filled - tx_filled)
    if sec["fills_pending"] > 1e-9:
        sec["complete"] = False

    # Futures: realized only. Open futures and futures orders are invisible to the API.
    fut_net: dict[str, float] = {}
    for f in fills:
        if f.asset_type == "FUTURE" and f.day == fut_today:
            fut_net[f.symbol] = fut_net.get(f.symbol, 0.0) + f.qty

    known = [t for t in closed_today if t.net is not None]
    open_by = {h: sum(p["open_pnl"] for p in positions if p["held"] == h)
               for h in ("INTRADAY", "MIXED", "CARRIED")}
    cb = acct.get("currentBalances") or {}

    return {
        "generated": now.isoformat(),
        "today": eq_today.isoformat(),
        "futures_session": fut_today.isoformat(),
        "auth_hours_left": auth_hours_left,
        "freshness": freshness,
        "history_complete": history_complete,
        "summary": {
            "realized": {
                "securities": sec, "futures": fut,
                "net": sec["net"] + fut["net"],
                "complete": sec["complete"] and fut["complete"],
            },
            "open_pnl": {
                "intraday": open_by["INTRADAY"], "mixed": open_by["MIXED"],
                "carried": open_by["CARRIED"], "positions": len(positions),
                "note": "since entry, Schwab basis; futures not included"},
            "trades_closed": len(closed_today),
            "wins": sum(1 for t in known if t.net > 0),
            "losses": sum(1 for t in known if t.net <= 0),
            "unknown": len(closed_today) - len(known),
            "fees_today": sec["fees"] + fut["fees"],
        },
        "positions": positions,
        "orders": [_order_row(p, quotes) for p in pend],
        "closed": [_trade_row(t) for t in closed_today],
        "futures": {
            "observed_net_today": {k: v for k, v in fut_net.items() if abs(v) > 1e-9},
            "note": "Schwab's API does not return futures positions or futures orders. "
                    "Open futures and their stops: check thinkorswim.",
        },
        "balances": {k: cb.get(k) for k in (
            "liquidationValue", "equity", "buyingPower", "dayTradingBuyingPower",
            "availableFunds", "maintenanceRequirement")},
    }
