"""The order ticket service: one click = one Schwab bracket (entry + stop + target).

Safety that is engineering, not coaching:
- Off until the user arms it in the dashboard (persisted). It never places orders on its own, with
  one exception the user opts into per ticket: Auto B/E moves that ticket's stop to break-even
  once, when the mark touches +1R (auto_breakeven, called by the poller).
- Separate Schwab client that can only preview and place; it cannot cancel or replace.
- Intent is written to SQLite BEFORE sending; a ticket id can be sent once (double clicks).
- One submission at a time. A timeout or 5xx is UNKNOWN, never retried: new submissions wait
  until the poller finds the order or the user marks it resolved after checking thinkorswim.
- Fresh quote at click time. Within LOCK_R of the risk per share (at least LOCK_MIN_TICKS), the
  click sends exactly what the ticket showed (shares, limit, stop, targets); if price moved further
  against the entry, or the stop is now at or through the bid / ask, nothing is sent and the
  ticket shows the new numbers.
- Regular hours only (stops do not trigger outside them).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from . import schwab
from .bracket import EQUITY, MIN_STOP_TICKS, Plan, _to_tick, bracket_order, fmt, plan, put_call, tick, ticks_for
from .orders import LIVE, TERMINAL, PendingOrder, pending_orders, stop_coverage, terminal_ids
from .pnl import ET
from .store import Store, now_iso

log = logging.getLogger("companion.trading")

LOCK_R = Decimal("0.1")       # the click keeps the displayed plan while price moved <= 0.1R against it
LOCK_MIN_TICKS = 2            # ...and at least 2 ticks (2 cents above $1; 2 option ticks for an option)
NO_STOP_LOCK = Decimal("0.001")   # no stop, no R: the lock is 0.1% of the price instead
RISK_CHOICES = (250, 375, 500, 750)      # defaults; the user edits them with the gear on the ticket
EXPIRATIONS_TTL = 300.0       # the option chain picker's expiry list, seconds
CHAIN_TTL = 5.0               # one expiry's strikes (bid / ask move; the click re-quotes anyway)
UNDERLYING_RE = re.compile(r"^\$?[A-Z][A-Z0-9./]{0,9}$")
# Schwab's market data names indexes with a "$" ($SPX); a bare SPX on /chains is a 400.
# A stop leg in one of these has been live at Schwab (ACCEPTED/QUEUED may still be rejected).
WORKED = frozenset({"WORKING", "AWAITING_STOP_CONDITION", "PENDING_REPLACE", "PENDING_CANCEL",
                    "FILLED", "CANCELED", "REPLACED", "EXPIRED"})
INDEX_ROOTS = frozenset({"SPX", "NDX", "RUT", "XSP", "VIX", "DJX", "OEX", "XND", "MRUT"})
OCC_RE = re.compile(r"^[A-Z0-9.]{1,6} {0,5}\d{6}[CP]\d{8}$")


def _dec(v) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except Exception:
        return None
    return d if d.is_finite() else None


def _mask_account(obj):
    if isinstance(obj, dict):
        return {k: ("***" if k == "accountNumber" else _mask_account(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_mask_account(v) for v in obj]
    return obj


OPENING_INSTRUCTIONS = {"SELL_SHORT", "BUY_TO_OPEN", "SELL_TO_OPEN"}


def unfilled_entry(o: dict) -> tuple[bool, str]:
    """Is this top-level order an entry that has not filled at all? Only those may be cancelled.

    A partly filled entry is refused: cancelling a partly filled bracket could also cancel the
    stop that protects the shares already bought.
    """
    if o.get("orderStrategyType") not in ("SINGLE", "TRIGGER"):
        return False, f"not a single entry or bracket ({o.get('orderStrategyType')})"
    if o.get("status") not in LIVE:
        return False, f"not working ({o.get('status')})"
    legs = o.get("orderLegCollection") or []
    if not legs:
        return False, "order has no legs"
    for leg in legs:
        effect, instr = leg.get("positionEffect"), leg.get("instruction")
        if effect == "CLOSING" or (effect != "OPENING" and instr not in OPENING_INSTRUCTIONS):
            return False, "not an entry (it closes or reduces a position)"
    filled = o.get("filledQuantity")
    if not isinstance(filled, (int, float)) or filled != filled or filled < 0:
        return False, "Schwab did not report a fill count, so it cannot be shown unfilled"
    if filled > 0:
        return False, f"already {filled:g} filled: cancel the rest in thinkorswim"
    if any(a.get("executionType") == "FILL" for a in o.get("orderActivityCollection") or []):
        return False, "Schwab shows executions on it"
    # A bracket whose stop or target is already live has (some) fills, whatever the count says.
    # Only the leaf stop/target orders are checked: they wait in AWAITING_PARENT_ORDER until the
    # entry fills (the OCO container's own status is not documented).
    for oco in o.get("childOrderStrategies") or []:
        for kid in oco.get("childOrderStrategies") or ([oco] if oco.get("orderType") else []):
            if kid.get("status") in LIVE:
                return False, "its stop or target is already live, so part of it filled"
    return True, ""


def _symbol(o: dict) -> str | None:
    legs = o.get("orderLegCollection") or []
    return (legs[0].get("instrument") or {}).get("symbol") if legs else None


def _replacement(tree: dict, orders: list[dict]) -> dict | None:
    """The order that replaced a REPLACED one (an edit in thinkorswim). Schwab gives no link
    field: the new order is entered the second the old one closes, same symbol and instruction.
    Returns None unless exactly one order fits."""
    try:
        closed = datetime.strptime(tree.get("closeTime") or "", "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None
    legs = tree.get("orderLegCollection") or [{}]
    hits = []
    for o in orders:
        ol = o.get("orderLegCollection") or [{}]
        if (o.get("orderId") == tree.get("orderId") or _symbol(o) != _symbol(tree)
                or ol[0].get("instruction") != legs[0].get("instruction")):
            continue
        try:
            entered = datetime.strptime(o.get("enteredTime") or "", "%Y-%m-%dT%H:%M:%S%z")
        except ValueError:
            continue
        if abs((entered - closed).total_seconds()) <= 2:
            hits.append(o)
    return hits[0] if len(hits) == 1 else None


def market_open(now: datetime | None = None) -> bool:
    et = (now or datetime.now(timezone.utc)).astimezone(ET)
    if et.weekday() >= 5:
        return False
    minutes = et.hour * 60 + et.minute
    return 9 * 60 + 30 <= minutes < 16 * 60


class _Throttled:
    """Stand-in response for a read skipped during a 429 pause (nothing was sent)."""
    status_code = 429
    headers: dict = {}
    text = "skipped during a rate-limit pause"
    content = b""

    def json(self):
        return {}

    def raise_for_status(self):
        raise RuntimeError("Schwab rate limit pause: try again in a few seconds")


def _basis_price(q: dict, basis: str):
    """The quote a Schwab trailing stop follows."""
    return {"MARK": q.get("mark") or q.get("last"), "LAST": q.get("last"), "BID": q.get("bid"),
            "ASK": q.get("ask")}.get((basis or "MARK").upper())


def _trail_txt(spec: dict) -> str:
    off = spec["offset"]
    d = Decimal(str(off))
    # Cents when the distance is whole cents (always for options), sub-penny stocks keep 4 decimals.
    dist = f"{off:g}%" if spec["link"] == "PERCENT" else f"${d:.2f}" if d == d.quantize(Decimal("0.01")) else f"${fmt(d)}"
    return f"trailing {dist} from {spec['basis'].lower()}"


def _units(asset: str | None) -> tuple[str, str]:
    """(word, short) for the position's size in messages."""
    return ("contracts", "ct") if asset == "OPTION" else ("shares", "sh")


def _occ_root(symbol: str) -> str:
    """The root of an OCC option symbol ("SPY   261120C00600000" -> "SPY")."""
    return symbol[:6].strip() if len(symbol) > 15 else symbol


def _odd_deliverable(q: dict, symbol: str = "") -> str | None:
    """Why an option is an adjusted contract (after a split, merger or spin-off), or None when it
    is standard. Adjusted contracts deliver something other than 100 shares of one underlying, so
    their prices do not compare with the regular chain and the ticket refuses them. Fails closed:
    only "100 <underlying>" (either order, "(Cash)" allowed for a cash-settled index like
    "100 $SPX(Cash)") or no deliverables at all counts as standard, and an OCC root ending in a
    digit (AAPL1) is an adjusted root."""
    root = _occ_root(symbol) if symbol else ""
    if root and root[-1].isdigit():
        return f"adjusted root {root}"
    d = q.get("deliverables")
    if d is None or (isinstance(d, str) and not d.strip()):
        return None
    if not isinstance(d, str):
        return f"deliverables {str(d)[:60]!r}"
    und = (q.get("underlying") or root or "").upper().strip().lstrip("$")
    parts = [x.lstrip("$").removesuffix("(CASH)") for x in d.upper().split()]
    if und and len(parts) == 2 and sorted(parts) == sorted(["100", und]):
        return None
    return f"deliverables {d.strip()[:60]!r}"


_ENTRY_INSTR = {"LONG": ("BUY", "BUY_TO_OPEN"), "SHORT": ("SELL_SHORT", "SELL_TO_OPEN")}


def _fill_times(order: dict) -> list[str]:
    """Execution times of an order's own fills (not its children's)."""
    return [e["time"] for a in order.get("orderActivityCollection") or [] if a.get("executionType") == "FILL"
            for e in a.get("executionLegs") or [] if e.get("time")]


def _foreign_entry(orders: list[dict], symbol: str, side: str, since: str, own: set[int]) -> int | None:
    """The id of an order outside `own` that filled an entry on `symbol` in `side`'s direction at or
    after `since` (an ISO time; "" means any time in `orders`), or None."""
    def walk(o):
        legs = o.get("orderLegCollection") or []
        if (o.get("orderId") not in own and float(o.get("filledQuantity") or 0) > 0
                and any((l.get("instrument") or {}).get("symbol") == symbol
                        and l.get("instruction") in _ENTRY_INSTR[side] for l in legs)
                and (not _fill_times(o) or any(ft >= since for ft in _fill_times(o)))):   # no time: counts
            return o.get("orderId")
        for ch in o.get("childOrderStrategies") or []:
            hit = walk(ch)
            if hit:
                return hit
        return None
    for o in orders:
        hit = walk(o)
        if hit:
            return hit
    return None


def _option_id(ins: dict) -> tuple[str | None, str | None]:
    """(underlying without "$", CALL | PUT) of a position's option instrument, from Schwab's
    fields or else from the OCC symbol (index roots SPXW / NDXP -> SPX / NDX). None where
    neither says."""
    m = re.match(r"^([A-Z0-9.]{1,6}) *\d{6}([CP])\d{8}$", (ins.get("symbol") or "").upper())
    root = m.group(1) if m else None
    if root and len(root) > 1 and root[:-1] in INDEX_ROOTS and root[-1] in "WP":
        root = root[:-1]
    und = (ins.get("underlyingSymbol") or "").upper().lstrip("$") or root
    pc = ins.get("putCall") or ({"C": "CALL", "P": "PUT"}[m.group(2)] if m else None)
    return und, pc


def _market_symbol(underlying: str) -> str:
    """The symbol Schwab's chain endpoints expect: SPX -> $SPX, anything else unchanged."""
    sym = (underlying or "").upper().strip()
    return "$" + sym if sym in INDEX_ROOTS else sym


def _greek(v, lo: float, hi: float):
    """Schwab sends -999 (and the odd absurd value) when it has no greek: show nothing then."""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return round(x, 3) if lo <= x <= hi else None


def parse_chain(body: dict, pc: str) -> dict:
    """One expiry of Schwab's option chain as compact rows for the picker, by strike."""
    rows = []
    for _exp, strikes in (body.get("callExpDateMap" if pc == "CALL" else "putExpDateMap") or {}).items():
        for _k, contracts in (strikes or {}).items():
            for c in contracts or []:
                bid, ask = c.get("bid") or 0, c.get("ask") or 0
                mark = c.get("mark") or ((bid + ask) / 2 if bid and ask else None)
                mult = c.get("multiplier")
                rows.append({
                    "symbol": c.get("symbol"), "description": c.get("description"),
                    "strike": c.get("strikePrice"), "bid": bid, "ask": ask, "mark": mark, "last": c.get("last"),
                    "delta": _greek(c.get("delta"), -1.0, 1.0), "iv": _greek(c.get("volatility"), 0.0, 1000.0),
                    "oi": c.get("openInterest"), "volume": c.get("totalVolume"),
                    "spread_pct": round((ask - bid) / mark * 100, 1) if mark and ask >= bid > 0 else None,
                    "itm": bool(c.get("inTheMoney")), "dte": c.get("daysToExpiration"),
                    "penny": bool(c.get("pennyPilot") or c.get("isPennyPilot")),
                    "standard": not (c.get("nonStandard") or c.get("mini")) and (mult in (None, 100, 100.0)),
                })
    rows = [r for r in rows if r["symbol"] and r["strike"] is not None]
    rows.sort(key=lambda r: r["strike"])
    und = body.get("underlying") or {}
    return {"underlying_price": body.get("underlyingPrice") or und.get("mark") or und.get("last"),
            "underlying_name": und.get("description"), "rows": rows}


class Trader:
    expected_hash: str | None = None   # set by the poller: orders only ever go to the shown account
    on_request = None                  # set by the poller: counts order traffic in its limiter
    cooldown_left = None               # set by the poller: seconds left of a 429 pause
    _waited_for = 0.0                  # the 429 pause this trader already waited on
    def __init__(self, store: Store):
        self.store = store
        self._client = None
        self._hash: str | None = None
        self._send_lock = threading.Lock()
        self._quote_cache: dict[str, tuple[float, dict]] = {}

    # ---- plumbing ----------------------------------------------------------

    def _ensure(self):
        if self._client is not None and self.expected_hash and self._hash != self.expected_hash:
            raise RuntimeError("the dashboard switched accounts: restart the companion before trading")
        if self._client is None:
            client = schwab.make_trading_client()
            r = client.linked_accounts()
            r.raise_for_status()
            hashes = [a.get("hashValue") for a in r.json()]
            if self.expected_hash:
                if self.expected_hash not in hashes:
                    raise RuntimeError("the account shown on the dashboard is not linked to this login")
                self._hash = self.expected_hash
            else:
                if len(hashes) != 1:
                    raise RuntimeError("several linked accounts and the dashboard has not picked one yet")
                self._hash = hashes[0]
            self._client = client
        return self._client

    @property
    def armed(self) -> bool:
        """Arming lasts for the session it was given in: it lapses at the 16:00 ET close
        and on every restart (app startup calls arm(False))."""
        if self.store.get_setting("trading_armed", "0") != "1":
            return False
        at = self.store.get_setting("trading_armed_at")
        if not at:
            return False
        armed_at = datetime.fromisoformat(at).astimezone(ET)
        close = armed_at.replace(hour=16, minute=0, second=0, microsecond=0)
        if armed_at >= close:
            close += timedelta(days=1)
        if datetime.now(ET) >= close:
            self.arm(False, reason="session close")
            return False
        return True

    def arm(self, on: bool, reason: str = "dashboard") -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.store.set_setting("trading_armed", "1" if on else "0")
        self.store.set_setting("trading_armed_at" if on else "trading_disarmed_at", now)
        self.store.set_setting("trading_arm_log", (f"{now} {'ARM' if on else 'DISARM'} ({reason})\n"
                                                   + (self.store.get_setting("trading_arm_log", "") or ""))[:4000])
        log.warning("trading %s (%s)", "ARMED" if on else "disarmed", reason)

    def _logged(self, endpoint: str, fn):
        """Run one Schwab request and record it in the same requests log as the poller."""
        started = now_iso()
        if endpoint not in ("place_order", "cancel_order", "replace_order") and self.cooldown_left:
            # During a 429 pause: status polling and quotes are not sent at all (the action then
            # treats the outcome as not confirmed and protects conservatively); the position read
            # that a rebuild needs waits at most 5 s, so a stop is never held back for long.
            left = self.cooldown_left()
            if left > 0:
                if endpoint in ("order_details", "ticket_quote", "option_expirations", "option_chain"):
                    return _Throttled()
                end = time.monotonic() + left
                if abs(end - self._waited_for) > 2.0:      # wait once per pause, at most 5 s
                    self._waited_for = end
                    time.sleep(min(left, 5.0))
        if self.on_request:
            self.on_request(None)
        try:
            r = fn()
        except Exception as e:
            self.store.log_request(endpoint, started, False, None, repr(e)[:300])
            raise
        if r.status_code == 429 and self.on_request:
            self.on_request(r.headers.get("Retry-After") or "30")
        ok = r.status_code in (200, 201)
        detail = None if ok else (f"Retry-After={r.headers.get('Retry-After')} " + r.text[:250])
        self.store.log_request(endpoint, started, ok, r.status_code, detail)
        return r

    def quote(self, symbol: str, max_age: float = 1.0) -> dict:
        sym = symbol.upper().strip()
        hit = self._quote_cache.get(sym)
        if hit and time.monotonic() - hit[0] < max_age:
            return hit[1]
        r = self._logged("ticket_quote", lambda: self._ensure().quotes([sym]))
        r.raise_for_status()
        entry = (r.json() or {}).get(sym) or {}
        q, ref = entry.get("quote") or {}, entry.get("reference") or {}
        asset = entry.get("assetMainType")
        underlying = ref.get("underlying") or (_occ_root(sym) if asset == "OPTION" else None)
        penny = ref.get("isPennyPilot")
        out = {"symbol": sym, "found": bool(q), "bid": q.get("bidPrice"), "ask": q.get("askPrice"),
               "last": q.get("lastPrice"), "mark": q.get("mark"), "change_pct": q.get("netPercentChange"),
               "name": ref.get("description"), "exchange": ref.get("exchangeName"),
               "asset_type": asset, "underlying": underlying, "multiplier": ref.get("multiplier"),
               "is_penny": penny if isinstance(penny, bool) else None, "realtime": entry.get("realtime"),
               "deliverables": ref.get("deliverables"), "put_call": put_call(sym) if asset == "OPTION" else None,
               "expiration": (f"{ref['expirationYear']:04d}-{ref['expirationMonth']:02d}-{ref['expirationDay']:02d}"
                              if all(isinstance(ref.get(k), int) for k in ("expirationYear", "expirationMonth",
                                                                         "expirationDay")) else None),
               "strike": ref.get("strikePrice"),
               "ticks": ticks_for(asset, underlying, penny if isinstance(penny, bool) else None)}
        self._quote_cache[sym] = (time.monotonic(), out)
        return out

    def _ticks(self, symbol: str, asset: str | None, q: dict | None = None) -> str:
        """The price grid for an action on this position. A stock never needs a quote for it; an
        option uses its quote's penny flag, and the nickel grid (valid everywhere) without one."""
        if asset != "OPTION":
            return EQUITY
        if q is None:
            try:
                q = self.quote(symbol)
            except Exception:
                q = {}
        if q.get("asset_type") == "OPTION" or q.get("is_penny") is not None:
            return ticks_for("OPTION", q.get("underlying") or _occ_root(symbol), q.get("is_penny"))
        return ticks_for("OPTION", _occ_root(symbol), None)

    def make_plan(self, req: dict, fresh: bool = False, q: dict | None = None, limit_override=None) -> Plan:
        """Asset, multiplier and price grid always come from Schwab's quote, never from the page."""
        q = q or self.quote(req["symbol"], max_age=0 if fresh else 1.0)
        asset = "OPTION" if q.get("asset_type") == "OPTION" else "EQUITY"
        mult = q.get("multiplier") or (100 if asset == "OPTION" else 1)
        odd = _odd_deliverable(q, req["symbol"]) if asset == "OPTION" else None
        p = plan(req["symbol"], req["side"], req["stop"], req["risk"], req.get("r", 2),
                 q.get("bid"), q.get("ask"), split=req.get("split") or 0, t1_r=req.get("t1_r") or 1,
                 entry=req.get("entry") or "cross", target_price=req.get("target_price"),
                 t1_price=req.get("t1_price"), stop_type=req.get("stop_type") or "fixed",
                 trail_unit=req.get("trail_unit") or "R", trail_value=req.get("trail_value") or 1,
                 trail_basis=req.get("trail_basis") or "MARK", limit_override=limit_override,
                 asset=asset, multiplier=mult, ticks=q.get("ticks") or ticks_for(asset),
                 size_mode=req.get("size_mode") or "risk", qty=req.get("qty"),
                 target=req.get("targets", 1) not in (0, "0"), limit_price=req.get("limit_price"))
        if odd:
            # Schwab's real deliverables format is unconfirmed: the reason is shown so a wrong refusal is easy to spot.
            p.errors.insert(0, f"non-standard contract ({odd}): not traded from the ticket")
        return p

    # ---- option chain picker (only on a click in the ticket's Option mode) ---------------------

    def expirations(self, underlying: str) -> list[dict]:
        sym = (underlying or "").upper().strip()
        if not UNDERLYING_RE.match(sym):
            raise ValueError("type a stock or ETF symbol")
        key = ("exp", sym)
        hit = self._quote_cache.get(key)
        if hit and time.monotonic() - hit[0] < EXPIRATIONS_TTL:
            return hit[1]
        r = self._logged("option_expirations",
                         lambda: self._ensure().option_expiration_chain(_market_symbol(sym)))
        r.raise_for_status()
        rows = [{"date": e.get("expirationDate"), "dte": e.get("daysToExpiration"),
                 "type": e.get("expirationType"), "standard": e.get("standard", True) is not False}
                for e in (r.json() or {}).get("expirationList") or [] if e.get("expirationDate")]
        rows.sort(key=lambda e: e["date"])
        self._quote_cache[key] = (time.monotonic(), rows)
        return rows

    def chain(self, underlying: str, expiration: str, put_call_: str, strikes: int = 16) -> dict:
        sym = (underlying or "").upper().strip()
        pc = (put_call_ or "").upper().strip()
        if not UNDERLYING_RE.match(sym):
            raise ValueError("type a stock or ETF symbol")
        if pc not in ("CALL", "PUT"):
            raise ValueError("pick Calls or Puts")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", expiration or ""):
            raise ValueError("pick an expiry")
        strikes = max(4, min(int(strikes or 16), 40))
        key = ("chain", sym, expiration, pc, strikes)
        hit = self._quote_cache.get(key)
        if hit and time.monotonic() - hit[0] < CHAIN_TTL:
            return hit[1]
        r = self._logged("option_chain", lambda: self._ensure().option_chains(
            _market_symbol(sym), contractType=pc, strikeCount=strikes, includeUnderlyingQuote=True,
            fromDate=expiration, toDate=expiration))
        r.raise_for_status()
        out = parse_chain(r.json() or {}, pc)
        out.update({"underlying": sym, "expiration": expiration, "put_call": pc})
        self._quote_cache[key] = (time.monotonic(), out)
        return out

    # ---- actions -----------------------------------------------------------

    def preview(self, req: dict) -> dict:
        p = self.make_plan(req)
        out = {"plan": p.as_dict()}
        if not p.ok:
            return out
        order = bracket_order(p)
        out["order"] = order
        try:
            r = self._logged("preview_order", lambda: self._ensure().preview_order(self._hash, order))
            body = r.json() if r.content and "json" in r.headers.get("Content-Type", "") else r.text[:2000]
            out["schwab_preview"] = {"http": r.status_code, "body": _mask_account(body)}
        except Exception as e:
            out["schwab_preview"] = {"error": repr(e)[:300]}
        return out

    def submit(self, req: dict) -> dict:
        """Send exactly the plan the ticket displayed, or nothing.

        req carries the displayed plan's own inputs (symbol, side, stop, risk, r) and outputs
        (expected_limit, expected_qty, expected_target) plus one ticket id per intent.
        """
        ticket_id = str(req.get("ticket_id") or "")
        if len(ticket_id) < 8:
            return {"ok": False, "error": "missing ticket id"}
        prior = self.store.get_ticket(ticket_id)
        if prior:   # same intent clicked again (double click, lost response): report, never resend
            return {"ok": prior["status"] == "ACCEPTED", "status": prior["status"], "duplicate": True,
                    "ticket_id": ticket_id, "order_id": prior["order_id"],
                    "error": None if prior["status"] == "ACCEPTED" else
                    f"This ticket was already sent: {prior['status']}. Check thinkorswim."}
        if not self.armed:
            return {"ok": False, "error": "Trading is not armed. Use the ARM switch on the ticket."}
        if not market_open():
            return {"ok": False, "error": "Regular hours only (9:30 to 16:00 ET)."}
        pending = self.store.open_tickets()
        if pending:
            t = pending[0]
            return {"ok": False, "error": f"Previous order ({t['symbol']} {t['qty']}) is not confirmed "
                    "yet. Check thinkorswim, then press Resolve.", "blocked_by": t["ticket_id"]}
        if not self._send_lock.acquire(blocking=False):
            return {"ok": False, "error": "Another submission is in progress."}
        state = {"sent": False}
        try:
            return self._submit_locked(req, ticket_id, state)
        except Exception as e:
            log.exception("submit failed")
            if not state["sent"]:
                return {"ok": False, "error": f"Not sent: {e}"[:300]}
            self._safe_update(ticket_id, "UNKNOWN", detail=repr(e)[:500])
            return {"ok": False, "status": "UNKNOWN", "ticket_id": ticket_id,
                    "error": "Error after sending. The order MAY be live: check thinkorswim."}
        finally:
            self._send_lock.release()

    def _safe_update(self, ticket_id: str, status: str, **kw) -> bool:
        try:
            self.store.update_ticket(ticket_id, status, **kw)
            return True
        except Exception:
            log.exception("could not record ticket %s as %s", ticket_id, status)
            return False

    def _submit_locked(self, req: dict, ticket_id: str, state: dict) -> dict:
        try:
            exp_limit = Decimal(str(req.get("expected_limit") or 0))
            exp_target = Decimal(str(req.get("expected_target") or 0))
            exp_qty = int(req.get("expected_qty") or 0)
        except Exception:
            exp_limit = exp_target = Decimal(0)
            exp_qty = 0
        if not (exp_limit.is_finite() and exp_target.is_finite()) or exp_limit <= 0 or exp_target < 0 or exp_qty <= 0:
            return {"ok": False, "error": "ticket is missing the displayed plan; review and click again"}
        q = self.quote(req["symbol"], max_age=0)
        now = self.make_plan(req, q=q)          # what a fresh ticket would show
        if not now.limit:
            return {"ok": False, "error": "; ".join(now.errors) or "no valid quote", "plan": now.as_dict()}
        # The plan the ticket showed, re-sized at the displayed entry price and checked against the
        # current bid / ask (the stop must still be on the right side of the market).
        p = self.make_plan(req, q=q, limit_override=exp_limit)
        if not p.ok:
            return {"ok": False, "error": "; ".join(p.errors), "plan": now.as_dict()}
        blocked = self._blocked(p.symbol, self._ensure())
        if blocked:
            return {"ok": False, "error": blocked["error"]}
        # Lock: a move against the entry up to 0.1R keeps the displayed plan. For a limit entry the
        # displayed limit caps the fill, so the risk cannot exceed what the ticket showed, and a
        # move in favour fills at a better price. A Mkt entry has no price cap: the lock only
        # refuses when the quote already sits more than 0.1R worse, and the fill can slip beyond.
        f, unit_w = p.f, ("contracts" if p.option else "shares")
        # Without a stop there is no R: the lock is 0.1% of the price (at least the 2 ticks).
        lock = max(LOCK_R * p.per_share if p.per_share else exp_limit * NO_STOP_LOCK,
                   LOCK_MIN_TICKS * tick(exp_limit, p.ticks))
        long = p.side == "LONG"
        against = now.limit - exp_limit if long else exp_limit - now.limit
        if against > lock:
            return {"ok": False, "error": f"Price moved: entry is now {f(now.limit)} (ticket showed "
                    f"{f(exp_limit)}, more than {f(lock)} away). Review and click again.",
                    "plan": now.as_dict()}
        # After a move in favour the fill comes nearer the stop: keep the minimum stop distance
        # from where it would fill now, not from the displayed price.
        room = (now.limit - p.stop if long else p.stop - now.limit) if p.stop else None
        if room is not None and room < MIN_STOP_TICKS * tick(now.limit, p.ticks):
            return {"ok": False, "error": f"Price is within {MIN_STOP_TICKS} ticks of the stop {f(p.stop)}. "
                    "Review and click again.", "plan": now.as_dict()}
        if p.size_mode == "qty" and p.qty != exp_qty:
            return {"ok": False, "error": f"The ticket now says {p.qty} {unit_w} (it showed {exp_qty}). "
                    "Review and click again.", "plan": now.as_dict()}
        if p.qty < exp_qty:
            return {"ok": False, "error": f"Size changed to {p.qty} {unit_w} (ticket showed {exp_qty}). "
                    "Review and click again.", "plan": now.as_dict()}
        if not p.has_stop and not req.get("confirm_no_stop"):
            # The page asks "Send without a stop?" on every click; a request without that answer is refused.
            return {"ok": False, "error": "Sending without a stop needs your confirmation. Click again and confirm.",
                    "plan": now.as_dict()}
        # Never more shares (contracts) than displayed; recompute the numbers for that size.
        p.qty = exp_qty
        p.planned_risk = p.per_share * p.qty * p.multiplier
        p.notional = p.limit * p.qty * p.multiplier
        # Same entry price and inputs give the same targets; a difference means the ticket's
        # inputs changed after it was displayed.
        if not p.has_target and exp_target:
            return {"ok": False, "error": "Ticket showed a target but the plan has none. Review.", "plan": now.as_dict()}
        if p.has_target and p.target != exp_target:
            return {"ok": False, "error": f"Target is now {f(p.target)} (ticket showed {f(exp_target) if exp_target else 'none'}). "
                    "Review and click again.", "plan": now.as_dict()}
        if p.two_targets:
            p.qty1 = int((p.qty * p.split).to_integral_value(rounding=ROUND_FLOOR))
            exp_qty1 = int(req.get("expected_qty1") or 0)
            exp_t1 = Decimal(str(req.get("expected_target1") or 0))
            if p.qty1 != exp_qty1 or p.qty1 < 1 or p.qty - p.qty1 < 1:
                return {"ok": False, "error": f"Split changed to {p.qty1}/{p.qty - p.qty1} {unit_w}. "
                        "Review and click again.", "plan": now.as_dict()}
            if p.target1 != exp_t1:
                return {"ok": False, "error": f"Target 1 is now {f(p.target1)}. Review and click again.",
                        "plan": now.as_dict()}
        elif req.get("expected_qty1"):
            return {"ok": False, "error": "Ticket showed two targets but the plan has one. Review.",
                    "plan": now.as_dict()}
        if p.trail:
            exp_off = Decimal(str(req.get("expected_trail_offset") or 0))
            if p.trail["offset"] != exp_off:
                return {"ok": False, "error": f"Trail distance is now {p.trail['offset']}. Review and click again.",
                        "plan": now.as_dict()}
            # The trail starts from Schwab's basis price after the fill: a wider spread or a moved
            # basis can start it further away than the ticket showed with the same distance.
            exp_start = _dec(req.get("expected_trail_start"))
            if exp_start is None or exp_start <= 0:
                return {"ok": False, "error": "Ticket is missing the trail start: reload the page.",
                        "plan": now.as_dict()}
            looser = exp_start - p.trail_start if long else p.trail_start - exp_start
            if looser > lock:
                return {"ok": False, "error": f"The trail would now start at {f(p.trail_start)} (ticket showed "
                        f"{f(exp_start)}). Review and click again.", "plan": now.as_dict()}
        elif req.get("expected_trail_offset"):
            return {"ok": False, "error": "Ticket showed a trailing stop but the plan has a fixed one. Review.",
                    "plan": p.as_dict()}
        order = bracket_order(p)
        if p.option and p.trail:
            # Schwab does not document trailing stops on options: its preview decides first.
            refused = self._preview_refusal(self._ensure(), order)
            if refused:
                rejected, why = refused
                head = (f"Schwab does not accept a trailing stop on this option: {why}" if rejected else
                        f"Schwab's preview did not confirm the trailing stop ({why})")
                return {"ok": False, "error": head.rstrip(".") + ". Nothing was sent: use a fixed stop or Auto B/E.",
                        "plan": now.as_dict()}
        intent = {"ticket_id": ticket_id, "symbol": p.symbol, "side": p.side, "qty": p.qty,
                  "limit": f(p.limit), "stop": f(p.stop) if p.stop else "", "target": f(p.target) if p.has_target else "",
                  "risk": float(p.planned_risk), "request": order}
        if p.stop_type == "autobe":
            self.store.add_auto_be(ticket_id, p.symbol)     # the user's consent for one stop move
        if not self.store.insert_ticket(intent):
            return {"ok": False, "error": "This ticket was already sent. Check thinkorswim."}
        log.warning("PLACE %s %s %s @ %s stop %s target %s", p.side, p.qty, p.symbol, p.limit,
                    p.stop, p.target)
        state["sent"] = True
        try:
            r = self._logged("place_order", lambda: self._ensure().place_order(self._hash, order))
        except Exception as e:   # timeout, connection reset: the order may or may not exist
            self._safe_update(ticket_id, "UNKNOWN", detail=repr(e)[:500])
            return {"ok": False, "status": "UNKNOWN", "ticket_id": ticket_id,
                    "error": "No answer from Schwab. The order MAY be live: check thinkorswim."}
        if r.status_code in (200, 201):
            tail = r.headers.get("Location", "").rstrip("/").split("/")[-1]
            oid = int(tail) if tail.isdigit() else None
            status = "ACCEPTED" if oid else "ACCEPTED_UNTRACKED"
            if not self._safe_update(ticket_id, status, order_id=oid):
                # The ticket stays SENDING (which blocks new tickets) until reconciled.
                return {"ok": False, "status": "UNKNOWN", "ticket_id": ticket_id, "order_id": oid,
                        "error": "Schwab accepted the order but the companion could not record it. "
                                 "Check thinkorswim."}
            return {"ok": True, "status": status, "order_id": oid, "ticket_id": ticket_id,
                    "plan": p.as_dict()}
        if r.status_code == 429:
            # Throttled before Schwab looked at the order: nothing was placed, a retry is safe
            # (it gets a new ticket id, so the record of this attempt stays intact).
            ra = r.headers.get("Retry-After")
            self._safe_update(ticket_id, "RATE_LIMITED", detail=f"Retry-After={ra} {r.text[:800]}")
            return {"ok": False, "status": "RATE_LIMITED", "ticket_id": ticket_id, "retry_after": ra,
                    "error": "Schwab throttled the order request (HTTP 429): nothing was placed. "
                             + (f"Retry in {ra} s." if ra else "Click again to retry.")}
        if 400 <= r.status_code < 500:
            # Schwab refused it: nothing was placed.
            self._safe_update(ticket_id, "REJECTED", detail=r.text[:1000])
            return {"ok": False, "status": "REJECTED", "ticket_id": ticket_id,
                    "error": f"Schwab rejected the order (HTTP {r.status_code}): {r.text[:300]}"}
        self._safe_update(ticket_id, "UNKNOWN", detail=f"HTTP {r.status_code}: {r.text[:500]}")
        return {"ok": False, "status": "UNKNOWN", "ticket_id": ticket_id,
                "error": f"Schwab answered HTTP {r.status_code}. The order MAY be live: check thinkorswim."}

    # ---- cancel unfilled entries -------------------------------------------

    def cancel_entry(self, order_id: int) -> dict:
        """Cancel one unfilled entry (and, for a bracket, its waiting stop and target)."""
        if not self._send_lock.acquire(blocking=False):
            return {"ok": False, "order_id": order_id, "error": "Another order action is in progress."}
        try:
            return self._cancel_locked(int(order_id))
        except Exception as e:
            log.exception("cancel failed")
            return {"ok": False, "order_id": order_id, "error": f"Cancel failed: {e}"[:300]}
        finally:
            self._send_lock.release()

    def _cancel_locked(self, order_id: int) -> dict:
        client = self._ensure()
        r = self._logged("order_details", lambda: client.order_details(self._hash, order_id))
        if r.status_code != 200:
            return {"ok": False, "order_id": order_id, "error": f"Could not read the order (HTTP {r.status_code})."}
        o = r.json()
        sym = _symbol(o)
        ok, why = unfilled_entry(o)
        if not ok:
            self.store.log_action("cancel_entry", sym, order_id, "REFUSED", why)
            return {"ok": False, "order_id": order_id, "symbol": sym, "error": f"Not cancelled: {why}."}
        self.store.log_action("cancel_entry", sym, order_id, "SENDING")
        try:
            r = self._logged("cancel_order", lambda: client.cancel_order(self._hash, order_id))
        except Exception as e:
            self.store.log_action("cancel_entry", sym, order_id, "UNKNOWN", repr(e))
            return {"ok": False, "order_id": order_id, "symbol": sym,
                    "error": "No answer from Schwab: the entry may or may not be cancelled. Check thinkorswim."}
        if r.status_code not in (200, 201, 204):
            # Typical: it filled a moment before the cancel arrived.
            self.store.log_action("cancel_entry", sym, order_id, "REJECTED", f"HTTP {r.status_code} {r.text[:300]}")
            return {"ok": False, "order_id": order_id, "symbol": sym,
                    "error": f"Schwab refused the cancel (HTTP {r.status_code}). It may have filled: check positions."}
        # Confirm: only CANCELED with zero fills counts as done. Up to ~3 s of read-only checks.
        status, filled, final = "UNCONFIRMED", None, {}
        for _ in range(10):
            time.sleep(0.25)
            after = self._logged("order_details", lambda: client.order_details(self._hash, order_id))
            if after.status_code != 200:
                continue
            final = after.json()
            status = final.get("status", "UNCONFIRMED")
            fq = final.get("filledQuantity")
            filled = float(fq) if isinstance(fq, (int, float)) else None
            if status in ("CANCELED", "FILLED", "REJECTED", "EXPIRED") or (filled or 0) > 0:
                break
        self.store.log_action("cancel_entry", sym, order_id, status, f"filled {filled}")
        if (filled or 0) > 0:
            # A fill beat the cancel. Say whether the filled shares still have a live stop.
            stops = [k for oco in final.get("childOrderStrategies") or []
                     for k in oco.get("childOrderStrategies") or [] if k.get("orderType") == "STOP"]
            live_stop = any(k.get("status") in LIVE for k in stops)
            what = ("Its stop is live." if live_stop else
                    "NO LIVE STOP on those shares: fix it in thinkorswim now." if stops else
                    "It had no attached stop: check the position.")
            return {"ok": False, "order_id": order_id, "symbol": sym, "status": status,
                    "error": f"{filled:g} shares filled before the cancel. {what}"}
        if status == "CANCELED" and filled == 0:
            return {"ok": True, "order_id": order_id, "symbol": sym, "status": status, "error": None}
        return {"ok": False, "order_id": order_id, "symbol": sym, "status": status,
                "error": f"Cancel requested; Schwab still shows {status}. It can fill until Schwab "
                         "confirms. Watch Pending orders or check thinkorswim."}

    def cancel_all_entries(self, orders: list[dict]) -> dict:
        """Cancel every unfilled entry in the given (top-level) orders, one at a time."""
        targets = [o["orderId"] for o in orders if unfilled_entry(o)[0]]
        results = [self.cancel_entry(oid) for oid in targets]
        return {"ok": all(r["ok"] for r in results), "count": len(targets), "results": results}

    # ---- symbol-level actions: cancel all of a symbol's orders, close the position ----------

    @staticmethod
    def _cancel_targets(symbol: str, orders: list[dict], known_terminal: set[int]) -> tuple[list[int], list[int]]:
        """Walk every order tree (descendants of finished parents included) and return
        (order ids to cancel so nothing stays working for this symbol, ids refused because they
        also involve another instrument, e.g. an option spread). A live order with legs is
        cancelled as a whole, which takes its waiting children with it; an OCO container has no
        legs, so its live stop/target leaves are cancelled one by one."""
        terminal = terminal_ids(orders) | known_terminal
        targets: list[int] = []
        mixed: list[int] = []

        def syms(node) -> set[str]:
            out = {(leg.get("instrument") or {}).get("symbol") for leg in node.get("orderLegCollection") or []}
            for ch in node.get("childOrderStrategies") or []:
                out |= syms(ch)
            return out

        def visit(node, covered: bool) -> None:
            oid = node.get("orderId")
            own = {(leg.get("instrument") or {}).get("symbol") for leg in node.get("orderLegCollection") or []}
            live = (bool(own) and node.get("status") in LIVE and oid not in terminal)
            if live and symbol in own and not covered:
                if syms(node) - {symbol}:
                    if oid not in mixed:
                        mixed.append(oid)
                    return
                if oid not in targets:
                    targets.append(oid)
                covered = True
            for ch in node.get("childOrderStrategies") or []:
                visit(ch, covered)

        for o in orders:
            visit(o, False)
        return targets, mixed

    def _fresh_orders(self, client, snapshot: list[dict] | None = None) -> list[dict]:
        """Today's orders read now (one call), merged over the poller's snapshot, which already
        holds older working GTC orders from its 59-day sweep. Fresh data wins."""
        now = datetime.now(timezone.utc)
        et_midnight = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        start = (et_midnight - timedelta(hours=6)).astimezone(timezone.utc)
        r = self._logged("account_orders", lambda: client.account_orders(self._hash, start, now))
        if r.status_code != 200:
            raise RuntimeError(f"could not read current orders (HTTP {r.status_code})")
        found = {o["orderId"]: o for o in snapshot or [] if o.get("orderId") is not None}
        found.update({o["orderId"]: o for o in r.json() or []})
        return list(found.values())

    def _cancel_fast(self, client, symbol: str, orders: list[dict], known_terminal: set[int]) -> dict:
        """Send every cancel for the symbol at once, then confirm with one or two fresh reads."""
        targets, mixed = self._cancel_targets(symbol, orders, known_terminal)
        sent: dict[int, int | None] = {}
        for oid in targets:
            try:
                c = self._logged("cancel_order", lambda o=oid: client.cancel_order(self._hash, o))
                sent[oid] = c.status_code
            except Exception as e:
                sent[oid] = None
                self.store.log_action("cancel_order", symbol, oid, "UNKNOWN", repr(e))
        status: dict[int, str] = {}
        for _ in range(3):
            if not sent:
                break
            time.sleep(0.35)
            fresh = {o["orderId"]: o for o in self._fresh_orders(client)}
            for oid in sent:
                node = fresh.get(oid) or self._find_in(fresh.values(), oid)
                if node is None:   # older than today: ask for it directly
                    r = self._logged("order_details", lambda o=oid: client.order_details(self._hash, o))
                    node = r.json() if r.status_code == 200 else {}
                status[oid] = node.get("status", "UNCONFIRMED")
            if all(s in TERMINAL for s in status.values()):
                break
        results = [{"order_id": oid, "status": status.get(oid, "UNCONFIRMED"), "http": code}
                   for oid, code in sent.items()]
        for x in results:
            self.store.log_action("cancel_order", symbol, x["order_id"], x["status"], f"http {x['http']}")
        pending = [x for x in results if x["status"] not in TERMINAL]
        return {"ok": not pending and not mixed, "results": results, "pending": pending,
                "mixed": mixed, "still_working": [x["order_id"] for x in pending]}

    @staticmethod
    def _find_in(orders, oid: int) -> dict | None:
        def walk(o):
            if o.get("orderId") == oid:
                return o
            for ch in o.get("childOrderStrategies") or []:
                hit = walk(ch)
                if hit:
                    return hit
            return None
        for o in orders:
            hit = walk(o)
            if hit:
                return hit
        return None

    def cancel_symbol_orders(self, symbol: str, orders: list[dict], known_terminal: set[int]) -> dict:
        """Cancel every working order for this symbol (entries, stops, targets). Position stays."""
        symbol = symbol.upper().strip()
        if not self._send_lock.acquire(blocking=False):
            return {"ok": False, "error": "Another order action is in progress."}
        try:
            client = self._ensure()
            res = self._cancel_fast(client, symbol, self._fresh_orders(client, orders), known_terminal)
            n = len(res["results"])
            parts = [f"No working {symbol} orders." if n == 0 else
                     f"{symbol}: {n - len(res['pending'])} of {n} orders cancelled."]
            if res["pending"]:
                parts.append("Some are still working: check thinkorswim.")
            if res["mixed"]:
                parts.append(f"{len(res['mixed'])} order(s) also involve another contract (spread): "
                             "not cancelled, do it in thinkorswim.")
            if n:
                parts.append(f"The {symbol} position has no stop from these orders now.")
            res["message"] = " ".join(parts)
            if not res["ok"]:
                res["error"] = res["message"]
            res["symbol"] = symbol
            return res
        except Exception as e:
            log.exception("cancel symbol failed")
            return {"ok": False, "symbol": symbol, "error": f"Cancel failed: {e}"[:300]}
        finally:
            self._send_lock.release()

    def _positions(self, client) -> list[dict]:
        r = self._logged("account_details", lambda: client.account_details(self._hash, fields="positions"))
        r.raise_for_status()
        return ((r.json() or {}).get("securitiesAccount") or {}).get("positions") or []

    def _position(self, client, symbol: str, positions: list[dict] | None = None) -> tuple[float, str | None]:
        for p in self._positions(client) if positions is None else positions:
            ins = p.get("instrument") or {}
            if ins.get("symbol") == symbol:
                return (float(p.get("longQuantity") or 0) - float(p.get("shortQuantity") or 0),
                        ins.get("assetType"))
        return 0.0, None

    @staticmethod
    def _leg_risk(symbol: str, qty: float, asset: str | None, positions: list[dict],
                  mixed: list[int]) -> str | None:
        """Why closing (part of) this position may split a spread or uncover a short, or None.
        An option leg is suspect when the account also holds the opposite side of the same type
        (call or put) on the same underlying: closing the long leaves that short uncovered,
        closing the short leaves the long alone. Long shares are suspect next to short calls
        (covered calls), short shares next to short puts. A working multi-leg order on the
        contract is suspect too. Positions alone cannot prove a pairing, so this asks, it never
        decides; the close itself is still one order on this symbol only."""
        def net(p):
            return float(p.get("longQuantity") or 0) - float(p.get("shortQuantity") or 0)

        def same(a, b):          # unknown on either side counts as a match: ask rather than miss
            return a is None or b is None or a == b

        me = next((p.get("instrument") or {} for p in positions
                   if (p.get("instrument") or {}).get("symbol") == symbol), {"symbol": symbol})
        if asset == "OPTION":
            und, pc = _option_id(me)
        else:
            und, pc = symbol.upper().lstrip("$"), None
        others = []
        for p in positions:
            ins = p.get("instrument") or {}
            n = net(p)
            if ins.get("symbol") == symbol or ins.get("assetType") != "OPTION" or abs(n) < 1e-9:
                continue
            o_und, o_pc = _option_id(ins)
            if not same(o_und, und):
                continue
            if asset == "OPTION":
                hit = same(o_pc, pc) and (n > 0) != (qty > 0)
            elif asset == "EQUITY":
                hit = n < 0 and same(o_pc, "CALL" if qty > 0 else "PUT")
            else:
                hit = False
            if hit:
                others.append((ins.get("symbol"), n))
        if not others and not mixed:
            return None
        held = ", ".join(f"{'LONG' if n > 0 else 'SHORT'} {abs(n):g} {s}" for s, n in others)
        if others and (asset == "EQUITY" or qty > 0) and any(n < 0 for _, n in others):
            why = f"You also hold {held}: closing {symbol} can leave that short UNCOVERED."
        elif others:
            why = f"You also hold {held}: this looks like one leg of a spread, closing it leaves the other leg alone."
        else:
            why = ""
        if mixed:
            why = (why + " " if why else "") + (f"{len(mixed)} working multi-leg order(s) include {symbol} "
                                                "(a spread order): Schwab may cancel or split them.")
        return why

    def close_position(self, symbol: str, orders: list[dict], known_terminal: set[int],
                       confirm_leg: bool = False) -> dict:
        """One market order for the whole position. Schwab cancels the symbol's other working
        orders (stop, target) itself when it enters (previewOrder warning, 2026-10-22), the way
        thinkorswim's Flatten works; the companion checks afterwards and cleans up anything left."""
        symbol = symbol.upper().strip()
        if not market_open():
            return {"ok": False, "symbol": symbol, "error": "Regular hours only (9:30 to 16:00 ET)."}
        if not self._send_lock.acquire(blocking=False):
            return {"ok": False, "symbol": symbol, "error": "Another order action is in progress."}
        try:
            return self._close_locked(symbol, orders, known_terminal, confirm_leg)
        except Exception as e:
            log.exception("close failed")
            return {"ok": False, "symbol": symbol, "error": f"Close failed: {e}. Check thinkorswim."[:300]}
        finally:
            self._send_lock.release()

    SETTLED = {"FLAT", "RATE_LIMITED", "REJECTED", "RESOLVED_MANUALLY", "DONE", "CHECK", "FINAL"}

    def _unresolved_close(self, symbol: str, client=None) -> dict | None:
        """The latest close or partial for this symbol, if its outcome is still open: SENDING /
        UNKNOWN (no answer), or a Schwab status that is not final (WORKING, QUEUED, SENT...). An
        open Schwab status is asked again first; once Schwab says it is final, it stops blocking.
        Until then no new close, partial, stop, target or trail is sent for the symbol."""
        start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        acts = [a for a in self.store.actions_since(start.isoformat())
                if a["kind"] in ("close", "partial") and a["symbol"] == symbol]
        if not acts:
            return None
        last = acts[-1]
        st = last["status"]
        if st in ("SENDING", "UNKNOWN"):
            return last
        if st in TERMINAL or st in self.SETTLED:
            return None
        oid = last.get("order_id")
        if oid and client is not None:
            try:
                d = self._logged("order_details", lambda: client.order_details(self._hash, oid))
                now_st = d.json().get("status") if d.status_code == 200 else None
            except Exception:
                now_st = None
            if now_st in TERMINAL:
                self.store.log_action(last["kind"], symbol, oid, now_st, "final status, checked again at Schwab")
                return None
        return last

    def _blocked(self, symbol: str, client) -> dict | None:
        prior = self._unresolved_close(symbol, client)
        if not prior:
            return None
        return {"ok": False, "symbol": symbol, "unresolved_close": True,
                "error": f"A previous {symbol} {prior['kind']} ({prior['created'][11:19]} UTC, {prior['status']}) has an "
                         "open outcome. Check thinkorswim, then press Resolve before sending another order for it."}

    def resolve_close(self, symbol: str) -> None:
        """the user checked thinkorswim after an unclear close or partial; allow orders again."""
        self.store.log_action("close", symbol.upper().strip(), None, "RESOLVED_MANUALLY", "resolved by the user")

    def _guard_leg(self, symbol: str, qty: float, asset: str | None, positions: list[dict],
                   orders: list[dict], known_terminal: set[int]) -> dict | None:
        """Refuse (until the user confirms) a close or partial that may split a spread or uncover a
        short. `orders` should be a fresh read for an option (working multi-leg orders on it).
        A spread order still on its way out (PENDING_CANCEL / PENDING_REPLACE) counts: it can fill."""
        terminal = terminal_ids(orders) | known_terminal
        mixed: list[int] = []

        def visit(node) -> None:
            syms = {(leg.get("instrument") or {}).get("symbol") for leg in node.get("orderLegCollection") or []}
            if (symbol in syms and syms - {symbol} and node.get("orderId") not in terminal
                    and node.get("status") in LIVE | {"PENDING_CANCEL", "PENDING_REPLACE"}):
                mixed.append(node.get("orderId"))
            for ch in node.get("childOrderStrategies") or []:
                visit(ch)

        for o in orders:
            visit(o)
        why = self._leg_risk(symbol, qty, asset, positions, mixed)
        if not why:
            return None
        log.warning("LEG GUARD %s: %s", symbol, why)
        return {"ok": False, "symbol": symbol, "needs_confirm": True, "error": why + " Nothing was sent."}

    def _close_locked(self, symbol: str, orders: list[dict], known_terminal: set[int],
                      confirm_leg: bool = False) -> dict:
        client = self._ensure()
        blocked = self._blocked(symbol, client)
        if blocked:
            return blocked
        positions = self._positions(client)
        qty, asset = self._position(client, symbol, positions)
        if abs(qty) < 1e-9:
            # Flat already: just make sure nothing is left working for the symbol.
            res = self._cancel_fast(client, symbol, self._fresh_orders(client, orders), known_terminal)
            self.store.log_action("close", symbol, None, "FLAT", f"nothing to close, {len(res['results'])} orders cancelled")
            return {"ok": res["ok"], "symbol": symbol, "message": f"{symbol} is already flat."
                    + (f" Cancelled {len(res['results'])} leftover order(s)." if res["results"] else ""),
                    "error": None if res["ok"] else "Some leftover orders are still working: check thinkorswim."}
        instr = {("EQUITY", True): "SELL", ("EQUITY", False): "BUY_TO_COVER",
                 ("OPTION", True): "SELL_TO_CLOSE", ("OPTION", False): "BUY_TO_CLOSE"}.get((asset, qty > 0))
        if not instr:
            return {"ok": False, "symbol": symbol, "error": f"Cannot close a {asset} position here."}
        if not confirm_leg:
            # Spread orders hold options: read them fresh only then, so a stock close stays as fast.
            seen = self._fresh_orders(client, orders) if asset == "OPTION" else orders
            guard = self._guard_leg(symbol, qty, asset, positions, seen, known_terminal)
            if guard:
                return guard
        size = abs(qty)
        order = {"orderStrategyType": "SINGLE", "orderType": "MARKET", "session": "NORMAL",
                 "duration": "DAY",
                 "orderLegCollection": [{"instruction": instr, "quantity": int(size) if size == int(size) else size,
                                         "instrument": {"symbol": symbol, "assetType": asset}}]}
        self.store.log_action("close", symbol, None, "SENDING", f"{instr} {size:g} MARKET")
        log.warning("CLOSE %s %s %s MARKET", instr, size, symbol)
        try:
            r = self._logged("place_order", lambda: client.place_order(self._hash, order))
        except Exception as e:
            self.store.log_action("close", symbol, None, "UNKNOWN", repr(e))
            return {"ok": False, "symbol": symbol, "unresolved_close": True,
                    "error": "No answer from Schwab on the close. It MAY have been sent (and your stop/target "
                             "cancelled by Schwab): check thinkorswim now."}
        if r.status_code == 429:
            self.store.log_action("close", symbol, None, "RATE_LIMITED", r.text[:300])
            return {"ok": False, "symbol": symbol,
                    "error": "Schwab throttled the close (HTTP 429): nothing was sent. Your stop/target are still working."}
        if r.status_code >= 500:
            self.store.log_action("close", symbol, None, "UNKNOWN", f"HTTP {r.status_code} {r.text[:300]}")
            return {"ok": False, "symbol": symbol, "unresolved_close": True,
                    "error": f"Schwab answered HTTP {r.status_code}: the close MAY have been sent. Check thinkorswim now."}
        if r.status_code not in (200, 201):
            self.store.log_action("close", symbol, None, "REJECTED", f"HTTP {r.status_code} {r.text[:300]}")
            return {"ok": False, "symbol": symbol,
                    "error": f"Schwab refused the close (HTTP {r.status_code}): {r.text[:200]}. "
                             "Your stop/target should still be working: check thinkorswim."}
        tail = r.headers.get("Location", "").rstrip("/").split("/")[-1]
        oid = int(tail) if tail.isdigit() else None
        final: dict = {}
        if oid:
            for _ in range(10):   # a market order usually fills well within a second
                time.sleep(0.3)
                d = self._logged("order_details", lambda: client.order_details(self._hash, oid))
                if d.status_code == 200:
                    final = d.json()
                    if final.get("status") in TERMINAL:
                        break
        filled = float(final.get("filledQuantity") or 0)
        legs = [e for a in final.get("orderActivityCollection") or [] if a.get("executionType") == "FILL"
                for e in a.get("executionLegs") or []]
        avg = (sum(e["price"] * e["quantity"] for e in legs) / sum(e["quantity"] for e in legs)) if legs else None
        status = final.get("status") or ("UNKNOWN" if oid is None else "SENT")
        self.store.log_action("close", symbol, oid, status,
                              f"{instr} {filled:g}/{size:g}" + (f" @ {avg:.4f}" if avg else ""))
        # Verify afterwards: anything still working for the symbol (Schwab should have cancelled
        # it) is cancelled now, and a flipped position is reported loudly.
        notes = []
        try:
            leftover = self._cancel_fast(client, symbol, self._fresh_orders(client, orders),
                                         known_terminal | ({oid} if oid else set()))
            if leftover["results"]:
                notes.append(f"Cancelled {len(leftover['results'])} {symbol} order(s) Schwab had left working.")
            if not leftover["ok"]:
                notes.append("Some orders are still working: check thinkorswim.")
            after, _ = self._position(client, symbol)
            if abs(after) > 1e-9 and (after > 0) != (qty > 0):
                notes.append(f"WARNING: you are now {'LONG' if after > 0 else 'SHORT'} {abs(after):g} {symbol} "
                             "(a stop likely filled at the same moment). Fix it in thinkorswim now.")
        except Exception as e:
            notes.append(f"Could not verify afterwards ({e}). Check thinkorswim.")
        done = filled >= size - 1e-9
        head = (f"Closed {symbol}: {instr.replace('_', ' ').lower()} {filled:g} @ {avg:.2f}." if done and avg
                else f"Close sent, {filled:g} of {size:g} filled so far (Schwab: {status}).")
        text = " ".join([head] + notes)
        ok = done and not any(n.startswith(("WARNING", "Some", "Could")) for n in notes)
        return {"ok": ok, "symbol": symbol, "order_id": oid, "filled": filled, "qty": size, "avg": avg,
                "unresolved_close": not done and status not in TERMINAL,
                "message": text, "error": None if ok else text}

    # ---- move stop (B/E or any price) ------------------------------------------------------

    def move_stop(self, symbol: str, new_stop, orders: list[dict], known_terminal: set[int]) -> dict:
        """Change the stop price of every live stop protecting this position, keeping each one
        inside its OCO (Schwab replace = PUT on the stop order). Quantity and everything else stay
        the same. Afterwards: verify the new stop is working and each OCO's target still is."""
        symbol = symbol.upper().strip()
        if not market_open():
            return {"ok": False, "symbol": symbol, "error": "Regular hours only (9:30 to 16:00 ET)."}
        if not self._send_lock.acquire(blocking=False):
            return {"ok": False, "symbol": symbol, "error": "Another order action is in progress."}
        try:
            return self._move_stop_locked(symbol, new_stop, orders, known_terminal)
        except Exception as e:
            log.exception("move stop failed")
            return {"ok": False, "symbol": symbol, "error": f"Move stop failed: {e}. Check thinkorswim."[:300]}
        finally:
            self._send_lock.release()

    def be_price(self, symbol: str, orders_today: list[dict], position_avg: float | None) -> float | None:
        """Break-even = the companion ticket's entry fill if it opened this position, else
        Schwab's average price."""
        b = self.r_basis(orders_today).get(symbol.upper().strip())
        return b["entry"] if b else position_avg

    def _move_stop_locked(self, symbol: str, new_stop, orders: list[dict], known_terminal: set[int],
                          only: str | None = None, allowed: set[int] | None = None) -> dict:
        """only = "LONG" | "SHORT": the automatic B/E move. Then nothing but an in-place replace of
        fixed stops on that side, each one inside the ticket's own order tree (allowed): no stop
        created, no trail rebuilt, and a stop that is already at or past the new price (checked on
        the final read of each stop) is left alone."""
        from .bracket import _to_tick
        client = self._ensure()
        blocked = self._blocked(symbol, client)
        if blocked:
            return blocked
        qty, asset = self._position(client, symbol)
        if abs(qty) < 1e-9:
            return {"ok": False, "symbol": symbol, "error": f"No open {symbol} position."}
        side = "LONG" if qty > 0 else "SHORT"
        if only and side != only:
            return {"ok": False, "symbol": symbol, "error": f"The position is {side} now, not {only}. Not moved."}
        q = self.quote(symbol, max_age=0)
        tk = self._ticks(symbol, asset, q)
        unit, us = _units(asset)

        def f(v) -> str:
            return fmt(v, tk)
        price = Decimal(str(new_stop))
        # An off-grid stop goes to the tighter tick, never the looser one (long up, short down).
        price = _to_tick(price, ROUND_CEILING if side == "LONG" else ROUND_FLOOR, tk)
        if price <= 0:
            return {"ok": False, "symbol": symbol, "error": "Enter a stop price above 0."}
        bid, ask = Decimal(str(q.get("bid") or 0)), Decimal(str(q.get("ask") or 0))
        if side == "LONG" and bid and price >= bid:
            return {"ok": False, "symbol": symbol,
                    "error": f"Stop {f(price)} is at or above the bid {f(bid)}: it would trigger now. Not moved."}
        if side == "SHORT" and ask and price <= ask:
            return {"ok": False, "symbol": symbol,
                    "error": f"Stop {f(price)} is at or below the ask {f(ask)}: it would trigger now. Not moved."}
        fresh = self._fresh_orders(client, orders)
        terminal = terminal_ids(fresh) | known_terminal
        from .orders import pending_orders
        stops = [p for p in pending_orders(fresh, terminal)
                 if p.symbol == symbol and p.role == "STOP" and p.live and p.legs == 1 and p.exits(side)]
        if only and (not stops or any(p.order_type.startswith("TRAILING") for p in stops)):
            return {"ok": False, "symbol": symbol, "error": "No fixed stop to move (none, or a trailing stop). Not moved."}
        if only and any(p.order_id not in (allowed or set()) for p in stops):
            return {"ok": False, "symbol": symbol, "error": "A working stop is not from this ticket's order. Not moved."}
        if not stops:
            return self._create_stop(client, symbol, side, qty, asset, price, fresh, terminal)
        if any(p.order_type.startswith("TRAILING") for p in stops):
            # A trailing stop cannot be replaced by a fixed one in place: send one new OCO (fixed
            # stop + the kept target); Schwab drops the trailing exits as it enters.
            pend = pending_orders(fresh, terminal)
            if any(p.symbol == symbol and p.live and p.role not in ("STOP", "TARGET") for p in pend):
                return {"ok": False, "symbol": symbol,
                        "error": f"{symbol} has other working orders that Schwab would cancel. Do this in thinkorswim."}
            _, target = self._exit_plan(pend, symbol, side, 0)
            self.store.log_action("move_stop", symbol, None, "SENDING", f"trailing -> fixed {f(price)}")
            return self._create_stop(client, symbol, side, abs(qty), asset, price, [], set(),
                                     target=Decimal(str(target)) if target is not None else None, rebuild=True)
        had_oco = any(p.oco_id is not None for p in stops)
        had_targets = any(p.symbol == symbol and p.role == "TARGET" and p.live and p.exits(side)
                          for p in pending_orders(fresh, terminal))
        results = []
        for p in stops:
            r = self._logged("order_details", lambda o=p.order_id: client.order_details(self._hash, o))
            cur = r.json() if r.status_code == 200 else {}
            if cur.get("status") not in LIVE:
                results.append({"order_id": p.order_id, "ok": False, "note": f"stop is {cur.get('status')}"})
                continue
            if only:
                was = _dec(cur.get("stopPrice"))
                if cur.get("orderType") not in ("STOP", "STOP_LIMIT") or was is None:
                    results.append({"order_id": p.order_id, "ok": False, "note": f"it is a {cur.get('orderType')} order now"})
                    continue
                if was >= price if side == "LONG" else was <= price:
                    results.append({"order_id": p.order_id, "ok": False, "note": f"already at {f(was)}, not loosened"})
                    continue
            legs = cur.get("orderLegCollection") or []
            body = {
                "orderStrategyType": "SINGLE", "orderType": cur.get("orderType", "STOP"),
                "session": cur.get("session", "NORMAL"), "duration": cur.get("duration", "DAY"),
                "stopPrice": f(price),
                "orderLegCollection": [{"instruction": l.get("instruction"),
                                        "quantity": int(l.get("quantity")) if float(l.get("quantity") or 0).is_integer()
                                        else l.get("quantity"),
                                        "instrument": {"symbol": (l.get("instrument") or {}).get("symbol"),
                                                       "assetType": (l.get("instrument") or {}).get("assetType", "EQUITY")}}
                                       for l in legs],
            }
            if cur.get("orderType") == "STOP_LIMIT" and cur.get("price") is not None:
                body["price"] = cur.get("price")
            self.store.log_action("move_stop", symbol, p.order_id, "SENDING",
                                  f"{f(Decimal(str(cur.get('stopPrice') or 0)))} -> {f(price)}")
            try:
                rr = self._logged("replace_order", lambda o=p.order_id, b=body: client.replace_order(self._hash, o, b))
            except Exception as e:
                self.store.log_action("move_stop", symbol, p.order_id, "UNKNOWN", repr(e))
                results.append({"order_id": p.order_id, "ok": False, "note": "no answer from Schwab"})
                continue
            if rr.status_code not in (200, 201):
                self.store.log_action("move_stop", symbol, p.order_id, "REJECTED", f"HTTP {rr.status_code} {rr.text[:200]}")
                results.append({"order_id": p.order_id, "ok": False,
                                 "note": f"Schwab refused (HTTP {rr.status_code}): {rr.text[:150]}"})
                continue
            tail = rr.headers.get("Location", "").rstrip("/").split("/")[-1]
            results.append({"order_id": p.order_id, "ok": True, "new_id": int(tail) if tail.isdigit() else None})
        # Verify: each new stop working at the new price, and the OCO's target still working.
        time.sleep(0.6)
        after = self._fresh_orders(client)
        live_stops = [p for p in pending_orders(after, terminal_ids(after))
                      if p.symbol == symbol and p.role == "STOP" and p.live and p.exits(side)]
        live_targets = [p for p in pending_orders(after, terminal_ids(after))
                        if p.symbol == symbol and p.role == "TARGET" and p.live and p.exits(side)]
        at_new = [p for p in live_stops if p.stop is not None and abs(Decimal(str(p.stop)) - price) < Decimal("0.00001")]
        moved = sum(1 for x in results if x["ok"])
        # The OCO check only applies when the stop was in an OCO before the move.
        oco_kept = (not had_oco) or (bool(at_new) and all(p.oco_id is not None for p in at_new))
        covered = sum(p.qty for p in at_new)
        tq = sum(p.qty for p in live_targets)
        notes = [f"Stop moved to {f(price)} on {covered:g} of {abs(qty):g} {unit}."]
        if had_oco:
            if live_targets and oco_kept:
                notes.append(f"OCO kept: target(s) still working ({tq:g} {us}) and linked to the stop.")
            elif live_targets:
                notes.append("WARNING: the new stop is NOT linked to the target (OCO broken): fix in thinkorswim.")
            else:
                notes.append("WARNING: the target is no longer working after the move. Check thinkorswim.")
        elif had_targets and not live_targets:
            notes.append("WARNING: a target was working before and is gone now. Check thinkorswim.")
        for x in results:
            if not x["ok"]:
                notes.append(f"Stop {x['order_id']} not moved: {x['note']}.")
        ok = (moved == len(stops) and covered >= abs(qty) - 1e-9 and oco_kept
              and (bool(live_targets) or not had_targets))
        self.store.log_action("move_stop", symbol, None, "DONE" if ok else "CHECK",
                              " ".join(notes)[:900])
        return {"ok": ok, "symbol": symbol, "stop": f(price), "moved": moved, "of": len(stops),
                "oco_kept": oco_kept, "message": " ".join(notes), "error": None if ok else " ".join(notes)}

    # ---- auto B/E: the ticket's one stop move at +1R ------------------------------------------

    def auto_breakeven(self, ticket_id: str, symbol: str, side: str, entry: float,
                       orders: list[dict], known_terminal: set[int]) -> dict:
        """Move the stop to break-even once, for a ticket sent with Auto B/E (the user's consent for
        this one move). The poller calls it after claiming the ticket (PENDING -> SENDING), so it
        never runs twice. Only "another action holds the lock" or "market closed" put it back to
        PENDING (nothing was sent); every other outcome is final, never retried."""
        symbol = symbol.upper().strip()
        long = side == "LONG"
        be = _to_tick(Decimal(str(entry)), ROUND_CEILING if long else ROUND_FLOOR)   # never below entry
        tk = EQUITY

        def done(state: str, text: str, ok: bool = False) -> dict:
            self.store.set_auto_be(ticket_id, state, text)
            self.store.log_action("auto_be", symbol, None, state, text)
            return {"ok": ok, "state": state, "message": text}

        if not market_open():
            self.store.set_auto_be(ticket_id, "PENDING", "market closed")
            return {"ok": False, "state": "PENDING"}
        if not self._send_lock.acquire(timeout=10):
            self.store.set_auto_be(ticket_id, "PENDING", "another order action was running")
            return {"ok": False, "state": "PENDING"}
        try:
            client = self._ensure()
            qty, asset = self._position(client, symbol)
            if abs(qty) < 1e-9 or (qty > 0) != long:
                return done("SKIPPED", f"no {side.lower()} {symbol} position any more")
            if asset == "OPTION":          # an option's B/E must sit on its own price grid
                tk = self._ticks(symbol, asset)
                be = _to_tick(Decimal(str(entry)), ROUND_CEILING if long else ROUND_FLOOR, tk)
            fresh = self._fresh_orders(client, orders)
            terminal = terminal_ids(fresh) | known_terminal
            # Only stops inside this ticket's own order tree (Schwab keeps a replaced stop there),
            # or in an exit OCO the companion itself rebuilt for it (new target, partial, set
            # stop): a re-entry or a stop rebuilt in thinkorswim is not what the user consented to move.
            t = self.store.get_ticket(ticket_id) or {}
            allowed: set[int] = set()

            def walk(o):
                if o.get("orderId") is not None:
                    allowed.add(o["orderId"])
                for ch in o.get("childOrderStrategies") or []:
                    walk(ch)
            roots = ([t["order_id"]] if t.get("order_id") else []) + self.store.auto_be_orders(ticket_id)
            first_fill = ""
            for i, oid in enumerate(roots):
                tree = self._find_in(fresh, oid)
                for _ in range(10):
                    if not tree or tree.get("status") != "REPLACED":
                        break
                    tree = _replacement(tree, fresh)
                if tree:
                    walk(tree)
                    if i == 0 and t.get("order_id"):
                        first_fill = min(_fill_times(tree), default="")
            # The position must still be the one the ticket opened: an entry fill on the symbol
            # from any other order since (an add, or a close and re-entry in thinkorswim) means
            # the user's consent no longer covers it. Unknown fill time: all of today's fills count.
            # Older GTC entries come from the poller's sweep and can be up to a sweep old: re-read
            # every entry on the symbol that still looks open (usually none) before trusting it.
            # Every node counts: an entry can sit inside an older OCO or TRIGGER group.
            open_entries: list[int] = []

            def entries(o):
                legs = o.get("orderLegCollection") or []
                if (o.get("orderId") is not None and o.get("orderId") not in allowed and o.get("status") in LIVE
                        and any((l.get("instrument") or {}).get("symbol") == symbol
                                and l.get("instruction") in _ENTRY_INSTR[side] for l in legs)):
                    open_entries.append(o["orderId"])
                for ch in o.get("childOrderStrategies") or []:
                    entries(ch)
            for o in fresh:
                entries(o)
            reread = []
            for oid in dict.fromkeys(open_entries):
                rr = self._logged("order_details", lambda o=oid: client.order_details(self._hash, o))
                if rr.status_code != 200:
                    return done("SKIPPED", f"could not re-read entry order {oid} on {symbol}: left as it is")
                reread.append(rr.json() or {})
            foreign = _foreign_entry(fresh + reread, symbol, side, first_fill, allowed)
            if foreign:
                return done("SKIPPED", f"the position was added to or re-entered by order {foreign} "
                                       "after this ticket's fill: left as it is")
            stops = [p for p in pending_orders(fresh, terminal)
                     if p.symbol == symbol and p.role == "STOP" and p.live and p.exits(side)]
            if not stops:
                return done("SKIPPED", "no working stop to move")
            if any(p.order_id not in allowed for p in stops):
                return done("SKIPPED", "the working stop is not from this ticket's order (a new trade, or a "
                                       "stop rebuilt in a new OCO): left as it is")
            if any(p.order_type.startswith("TRAILING") for p in stops):
                return done("SKIPPED", "the stop is a trailing stop now: left as it is")
            past = [p for p in stops if p.stop is not None
                    and (Decimal(str(p.stop)) >= be if long else Decimal(str(p.stop)) <= be)]
            if len(past) == len(stops):
                return done("DONE", f"stop already at or past B/E {fmt(be, tk)}", ok=True)
            if past:
                return done("SKIPPED", f"a stop is already past B/E {fmt(be, tk)}: moving all would loosen it")
            self.store.log_action("auto_be", symbol, None, "SENDING", f"+1R touched: stop to B/E {fmt(be, tk)}")
            r = self._move_stop_locked(symbol, be, fresh, terminal, only=side, allowed=allowed)
            return done("DONE" if r["ok"] else "FAILED",
                        (r.get("message") or r.get("error") or "")[:900], ok=r["ok"])
        except Exception as e:
            log.exception("auto B/E failed")
            return done("FAILED", f"{e}. Check thinkorswim."[:300])
        finally:
            self._send_lock.release()

    # ---- trailing stop (Schwab-held) -----------------------------------------------------------

    @staticmethod
    def _trail_of(pend: list[PendingOrder], symbol: str, side: str) -> dict | None:
        """The live trailing stop's settings for this position, if its stop is a trailing stop."""
        for p in pend:
            if (p.symbol == symbol and p.live and p.role == "STOP" and p.order_type.startswith("TRAILING")
                    and p.exits(side) and p.trail_offset):
                return {"offset": Decimal(str(p.trail_offset)), "link": p.trail_link or "VALUE",
                        "basis": p.trail_basis or "MARK"}
        return None

    def set_trail(self, symbol: str, unit: str, value, basis: str, orders: list[dict], known_terminal: set[int],
                  rbasis: dict | None, prior_trail_level=None) -> dict:
        """Replace the position's exits with one OCO: the kept target + a Schwab TRAILING_STOP for the
        whole size. A trail that starts looser than the current stop is allowed (the user's call,
        2026-10-25) and reported in the result."""
        symbol = symbol.upper().strip()
        if not market_open():
            return {"ok": False, "symbol": symbol, "error": "Regular hours only (9:30 to 16:00 ET)."}
        if not self._send_lock.acquire(blocking=False):
            return {"ok": False, "symbol": symbol, "error": "Another order action is in progress."}
        try:
            return self._set_trail_locked(symbol, unit, value, basis, orders, known_terminal, rbasis, prior_trail_level)
        except Exception as e:
            log.exception("trail failed")
            return {"ok": False, "symbol": symbol, "error": f"Trail failed: {e}. Check thinkorswim."[:300]}
        finally:
            self._send_lock.release()

    def _set_trail_locked(self, symbol, unit, value, basis, orders, known_terminal, rbasis, prior_trail_level=None) -> dict:
        from .bracket import trail_level, trail_spec
        from .orders import pending_orders
        client = self._ensure()
        blocked = self._blocked(symbol, client)
        if blocked:
            return blocked
        qty, asset = self._position(client, symbol)
        if abs(qty) < 1e-9:
            return {"ok": False, "symbol": symbol, "error": f"No open {symbol} position."}
        if asset not in ("EQUITY", "OPTION"):
            return {"ok": False, "symbol": symbol, "error": f"Trailing stops are not available for a {asset} position here."}
        side = "LONG" if qty > 0 else "SHORT"
        q = self.quote(symbol, max_age=0)
        tk = self._ticks(symbol, asset, q)
        unit_w, us = _units(asset)
        rps = None
        if (unit or "").upper() == "R":
            if not rbasis or rbasis.get("side") != side or not rbasis.get("rps"):
                return {"ok": False, "symbol": symbol,
                        "error": "A trail in R needs the frozen 1R of a companion entry. Use $ or %."}
            rps = Decimal(str(rbasis["rps"]))
        spec, err = trail_spec(unit, value, rps, basis, tk)
        if err:
            return {"ok": False, "symbol": symbol, "error": err}
        fresh = self._fresh_orders(client, orders)
        pend = pending_orders(fresh, terminal_ids(fresh) | known_terminal)
        others = [p for p in pend if p.symbol == symbol and p.live and p.role not in ("STOP", "TARGET")]
        if others:
            return {"ok": False, "symbol": symbol,
                    "error": f"{symbol} has other working orders ({len(others)}) that Schwab would cancel. "
                             "Set this trail in thinkorswim."}
        stop, target = self._exit_plan(pend, symbol, side, 0)
        if stop is None and prior_trail_level is not None:
            stop = prior_trail_level          # changing a trail: compare with where it has reached
        ref = Decimal(str(_basis_price(q, spec["basis"]) or 0))
        start = trail_level(ref, spec, side, tk) if ref else None
        looser = (start is not None and stop is not None
                  and (start < Decimal(str(stop)) if side == "LONG" else start > Decimal(str(stop))))
        note = (f" It starts about {fmt(start, tk)}, looser than your stop {fmt(Decimal(str(stop)), tk)} by "
                f"{fmt(abs(start - Decimal(str(stop))), tk)}/{us}." if looser else "")
        self.store.log_action("trail", symbol, None, "SENDING",
                              f"{_trail_txt(spec)}, {abs(qty):g} {us}" + (f", start ~{fmt(start, tk)}" if start else ""))
        # Schwab's API has no documented answer on trailing stops for options: ask its preview
        # first and send nothing it would refuse (the working stop and target stay as they are).
        res = self._create_stop(client, symbol, side, abs(qty), asset, None, [], set(),
                                target=Decimal(str(target)) if target is not None else None, rebuild=True, trail=spec,
                                preview=asset == "OPTION")
        if res.get("not_sent"):
            self.store.log_action("trail", symbol, None, "REJECTED", res["error"])
            return {"ok": False, "symbol": symbol, "error": res["error"]}
        msg = (res.get("message") or res.get("error") or "Not confirmed: check thinkorswim.") + note
        self.store.log_action("trail", symbol, None, "DONE" if res.get("ok") else "CHECK", msg)
        return {"ok": bool(res.get("ok")), "symbol": symbol, "message": msg, "looser": looser,
                "start": fmt(start, tk) if start else None, "error": None if res.get("ok") else msg}

    # ---- set target (price or R) -----------------------------------------------------------

    def _restart_note(self, symbol: str, side: str, trail: dict, prior_level) -> str:
        """A rebuilt trailing stop starts again from the current price; say when that is
        looser than where the old one had trailed to."""
        from .bracket import trail_level
        if prior_level is None:
            return " The trail restarts from the current price."
        q = self.quote(symbol, max_age=0)
        ref = _basis_price(q, trail["basis"])
        if not ref:
            return " The trail restarts from the current price."
        tk = q.get("ticks") or EQUITY
        start = trail_level(Decimal(str(ref)), trail, side, tk)
        prior = Decimal(str(prior_level))
        looser = start < prior if side == "LONG" else start > prior
        return (f" The trail restarts about {fmt(start, tk)}, looser than the ~{fmt(prior, tk)} it had reached."
                if looser else f" The trail restarts about {fmt(start, tk)}.")

    def set_target(self, symbol: str, price, r_mult, orders: list[dict], known_terminal: set[int],
                   basis: dict | None, prior_trail_level=None) -> dict:
        """Put one target on the whole position, at a price or at R x the frozen risk (entry fill
        and original stop of the companion ticket). Sent as ONE new OCO with the current stop for
        the full size; Schwab drops the old exits as it enters, so the stop is never removed first."""
        symbol = symbol.upper().strip()
        if not market_open():
            return {"ok": False, "symbol": symbol, "error": "Regular hours only (9:30 to 16:00 ET)."}
        if not self._send_lock.acquire(blocking=False):
            return {"ok": False, "symbol": symbol, "error": "Another order action is in progress."}
        try:
            return self._set_target_locked(symbol, price, r_mult, orders, known_terminal, basis, prior_trail_level)
        except Exception as e:
            log.exception("set target failed")
            return {"ok": False, "symbol": symbol, "error": f"Set target failed: {e}. Check thinkorswim."[:300]}
        finally:
            self._send_lock.release()

    def _set_target_locked(self, symbol, price, r_mult, orders, known_terminal, basis, prior_trail_level=None) -> dict:
        from decimal import ROUND_CEILING, ROUND_HALF_UP
        from .bracket import _to_tick
        from .orders import pending_orders
        client = self._ensure()
        blocked = self._blocked(symbol, client)
        if blocked:
            return blocked
        qty, asset = self._position(client, symbol)
        if abs(qty) < 1e-9:
            return {"ok": False, "symbol": symbol, "error": f"No open {symbol} position."}
        side = "LONG" if qty > 0 else "SHORT"
        q = self.quote(symbol, max_age=0)
        tk = self._ticks(symbol, asset, q)
        unit_w, us = _units(asset)

        def f(v) -> str:
            return fmt(v, tk)
        if r_mult:
            b = basis
            if not b or b.get("side") != side or not b.get("rps"):
                return {"ok": False, "symbol": symbol,
                        "error": "R needs the entry and original stop of a companion ticket. Type a price instead."}
            move = Decimal(str(b["rps"])) * Decimal(str(r_mult))
            raw = Decimal(str(b["entry"])) + (move if side == "LONG" else -move)
            target = _to_tick(raw, ROUND_FLOOR if side == "LONG" else ROUND_CEILING, tk)
        else:
            try:
                target = Decimal(str(price))
                on_grid = target.is_finite() and target > 0 and _to_tick(target, ROUND_HALF_UP, tk) == target
            except Exception:
                return {"ok": False, "symbol": symbol, "error": "Enter a target price."}
            if not on_grid:       # a typed price is sent as typed or not at all, like the ticket
                return {"ok": False, "symbol": symbol,
                        "error": f"Target must be a valid price in "
                                 f"{f(tick(target if target.is_finite() and target > 0 else Decimal(1), tk))} steps."}
        bid, ask = Decimal(str(q.get("bid") or 0)), Decimal(str(q.get("ask") or 0))
        if target <= 0 or (side == "LONG" and ask and target <= ask) or (side == "SHORT" and bid and target >= bid):
            return {"ok": False, "symbol": symbol,
                    "error": f"Target {f(target)} is at or through the market ({f(bid)} / {f(ask)}): "
                             "it would fill now. Use a partial or Close instead."}
        fresh = self._fresh_orders(client, orders)
        pend = pending_orders(fresh, terminal_ids(fresh) | known_terminal)
        others = [p for p in pend if p.symbol == symbol and p.live and p.role not in ("STOP", "TARGET")]
        if others:
            return {"ok": False, "symbol": symbol,
                    "error": f"{symbol} has other working orders ({len(others)}) that Schwab would cancel. "
                             "Set this target in thinkorswim."}
        stop, _ = self._exit_plan(pend, symbol, side, 0)
        trail = self._trail_of(pend, symbol, side)
        label = f"{r_mult:g}R = {f(target)}" if r_mult else f(target)
        if trail:
            self.store.log_action("set_target", symbol, None, "SENDING", f"target {label} + {_trail_txt(trail)}")
            res = self._create_stop(client, symbol, side, abs(qty), asset, None, [], set(), target=target,
                                    rebuild=True, trail=trail)
            msg = (res["message"] if res.get("closed") else
                   (f"Target set at {label} with the trailing stop ({_trail_txt(trail)}) as one OCO."
                    if res.get("ok") else res.get("error") or res.get("message") or "Not confirmed: check thinkorswim.")
                   + self._restart_note(symbol, side, trail, prior_trail_level))
            self.store.log_action("set_target", symbol, None, "DONE" if res.get("ok") else "CHECK", msg)
            return {"ok": bool(res.get("ok")), "symbol": symbol, "target": f(target), "message": msg,
                    "error": None if res.get("ok") else msg}
        if stop is None:
            # No stop to pair it with: a lone target (the row keeps showing NO STOP SEEN).
            instr = {("EQUITY", True): "SELL", ("EQUITY", False): "BUY_TO_COVER",
                     ("OPTION", True): "SELL_TO_CLOSE", ("OPTION", False): "BUY_TO_CLOSE"}.get((asset, side == "LONG"))
            self.store.log_action("set_target", symbol, None, "SENDING", f"target {label}, no stop")
            r = self._place_exit(client, symbol, instr, asset, int(abs(qty)), target)
            ok = r.status_code in (200, 201)
            msg = (f"Target set at {label} for {abs(qty):g} {unit_w}. WARNING: there is no stop: use Stop to B/E "
                   "or Move stop." if ok else f"Schwab refused the target (HTTP {r.status_code}): {r.text[:150]}")
            self.store.log_action("set_target", symbol, None, "DONE" if ok else "REJECTED", msg)
            return {"ok": ok, "symbol": symbol, "message": msg, "error": None if ok else msg}
        self.store.log_action("set_target", symbol, None, "SENDING", f"target {label} + stop {stop}, {abs(qty):g} {us}")
        res = self._create_stop(client, symbol, side, abs(qty), asset, Decimal(str(stop)), [], set(),
                                target=target, rebuild=True)
        msg = (res["message"] if res.get("closed") else
               f"Target set at {label} with stop {stop} for {abs(qty):g} {unit_w} as one OCO." + res.get("note", "")
               if res.get("ok") else res.get("error") or res.get("message") or "Not confirmed: check thinkorswim.")
        self.store.log_action("set_target", symbol, None, "DONE" if res.get("ok") else "CHECK", msg)
        return {"ok": bool(res.get("ok")), "symbol": symbol, "target": f(target), "message": msg,
                "error": None if res.get("ok") else msg}

    # ---- partials --------------------------------------------------------------------------

    def take_partial(self, symbol: str, pct: float | None, shares: float | None,
                     orders: list[dict], known_terminal: set[int], mode: str = "mid",
                     prior_trail_level=None, confirm_leg: bool = False) -> dict:
        """Sell (or cover) part of the position at market, then put the stop and target back on
        the shares that are left, at the same prices, as one OCO.

        Schwab cancels the symbol's working orders when the partial enters (same as thinkorswim),
        so protection is rebuilt right after the fill: about a second without a stop on the
        remaining shares instead of a manual re-entry. 100% (or all shares) is a Close."""
        symbol = symbol.upper().strip()
        if not market_open():
            return {"ok": False, "symbol": symbol, "error": "Regular hours only (9:30 to 16:00 ET)."}
        if not self._send_lock.acquire(blocking=False):
            return {"ok": False, "symbol": symbol, "error": "Another order action is in progress."}
        try:
            return self._partial_locked(symbol, pct, shares, orders, known_terminal,
                                        mode if mode in ("mid", "market", "ask", "bid") else "mid", prior_trail_level,
                                        confirm_leg)
        except Exception as e:
            log.exception("partial failed")
            return {"ok": False, "symbol": symbol,
                    "error": f"Partial failed: {e}. Check the position and its stop in thinkorswim."[:300]}
        finally:
            self._send_lock.release()

    @staticmethod
    def _exit_plan(pend: list[PendingOrder], symbol: str, side: str, sell: float) -> tuple[float | None, float | None]:
        """(stop, target) to rebuild after selling `sell` shares. The nearest target's shares are
        the ones taken; the rest keep the farthest remaining target and the most protective stop."""
        mine = [p for p in pend if p.symbol == symbol and p.live and p.legs == 1 and p.exits(side)]
        stops = [p.stop for p in mine if p.role == "STOP" and p.stop is not None]
        stop = (max(stops) if side == "LONG" else min(stops)) if stops else None
        targets = sorted(((p.price, p.qty) for p in mine if p.role == "TARGET" and p.price is not None),
                         key=lambda t: t[0], reverse=(side == "SHORT"))   # nearest first
        left = sell
        remaining = []
        for price, q in targets:
            take = min(q, left)
            left -= take
            if q - take > 1e-9:
                remaining.append(price)
        if not remaining and targets:
            remaining = [targets[-1][0]]   # all targeted shares sold: keep the farthest target
        target = remaining[-1] if remaining else None
        return stop, target

    MID_WAIT_S = 3.0      # how long a mid-price partial may rest before the rest goes at market

    def _wait_fill(self, client, oid: int, seconds: float) -> dict:
        final: dict = {}
        for _ in range(max(1, int(seconds / 0.25))):
            time.sleep(0.25)
            d = self._logged("order_details", lambda: client.order_details(self._hash, oid))
            if isinstance(d, _Throttled):
                break                     # rate-limit pause: stop polling, outcome stays unconfirmed
            if d.status_code == 200:
                final = d.json()
                if final.get("status") in TERMINAL:
                    break
        return final

    @staticmethod
    def _fills(final: dict) -> list[tuple[float, float]]:
        return [(float(e["quantity"]), float(e["price"])) for a in final.get("orderActivityCollection") or []
                if a.get("executionType") == "FILL" for e in a.get("executionLegs") or []]

    def _place_exit(self, client, symbol: str, instr: str, asset: str, n: int, price: Decimal | None):
        order = {"orderStrategyType": "SINGLE", "orderType": "LIMIT" if price else "MARKET",
                 "session": "NORMAL", "duration": "DAY",
                 "orderLegCollection": [{"instruction": instr, "quantity": n,
                                         "instrument": {"symbol": symbol, "assetType": asset}}]}
        if price:
            order["price"] = fmt(price, ticks_for(asset))
        return self._logged("place_order", lambda: client.place_order(self._hash, order))

    def _send_partial(self, client, symbol: str, instr: str, asset: str, n: int, mode: str) -> dict:
        """Mid / Ask / Bid: a limit at that price (the mid rounded one tick toward a fill), wait up
        to MID_WAIT_S, cancel what did not fill and send it at market (the shares left have no stop
        while it rests, so the wait stays short). Market: market at once.
        Schwab cancels the stop/target the moment the first order enters, so the wait is time
        without a stop; it is kept short."""
        from .bracket import _to_tick, tick
        selling = instr.startswith("SELL")
        price = None
        where = mode
        tk = ticks_for(asset)
        if mode in ("mid", "ask", "bid"):
            q = self.quote(symbol, max_age=0)
            tk = self._ticks(symbol, asset, q)
            bid, ask = Decimal(str(q.get("bid") or 0)), Decimal(str(q.get("ask") or 0))
            if mode == "mid" and bid > 0 and ask > bid + tick(bid, tk):
                price = _to_tick((bid + ask) / 2, ROUND_FLOOR if selling else "ROUND_CEILING", tk)
            elif mode == "bid" and bid > 0:
                price = _to_tick(bid, ROUND_FLOOR, tk)          # literal bid (takes for a sell, rests for a cover)
            elif mode == "ask" and ask > 0:
                price = _to_tick(ask, "ROUND_CEILING", tk)      # literal ask (rests for a sell, takes for a cover)
        how = f"limit {fmt(price, tk)} at the {where}" if price else "market"
        uncertain_rest = False
        self.store.log_action("partial", symbol, None, "SENDING", f"{instr} {n} {how}")
        log.warning("PARTIAL %s %s %s %s", instr, n, symbol, how)
        try:
            r = self._place_exit(client, symbol, instr, asset, n, price)
        except Exception as e:
            self.store.log_action("partial", symbol, None, "UNKNOWN", repr(e))
            return {"unresolved": True,
                    "error": "No answer from Schwab on the partial. It MAY have been sent, and Schwab may "
                             "have cancelled your stop/target: check thinkorswim now, then press Resolve."}
        if r.status_code == 429 or 400 <= r.status_code < 500:
            self.store.log_action("partial", symbol, None, "REJECTED", f"HTTP {r.status_code} {r.text[:200]}")
            return {"error": f"Schwab refused the partial (HTTP {r.status_code}): {r.text[:150]}. "
                             "Nothing was sent; your stop/target should still be working."}
        if r.status_code not in (200, 201):
            self.store.log_action("partial", symbol, None, "UNKNOWN", f"HTTP {r.status_code} {r.text[:200]}")
            return {"unresolved": True,
                    "error": f"Schwab answered HTTP {r.status_code} on the partial: it MAY have been sent. "
                             "Check thinkorswim now, then press Resolve."}
        tail = r.headers.get("Location", "").rstrip("/").split("/")[-1]
        oid = int(tail) if tail.isdigit() else None
        final = self._wait_fill(client, oid, self.MID_WAIT_S if price else 2.5) if oid else {}
        fills = self._fills(final)
        filled = sum(q for q, _ in fills)
        if price and oid and filled < n - 1e-9 and final.get("status") not in TERMINAL:
            # Mid did not fill in time: cancel the rest, then send it at market.
            try:
                self._logged("cancel_order", lambda: client.cancel_order(self._hash, oid))
            except Exception:
                pass
            final = self._wait_fill(client, oid, 1.5)
            fills = self._fills(final)
            filled = sum(q for q, _ in fills)
            rest = int(n - filled)
            if final.get("status") in TERMINAL and rest > 0:
                rest_ok = False
                try:
                    r2 = self._place_exit(client, symbol, instr, asset, rest, None)
                except Exception:
                    r2 = None
                tail2 = r2.headers.get("Location", "").rstrip("/").split("/")[-1] if r2 is not None and r2.status_code in (200, 201) else ""
                if tail2.isdigit():
                    final2 = self._wait_fill(client, int(tail2), 2.5)
                    fills += self._fills(final2)
                    filled = sum(q for q, _ in fills)
                    rest_ok = final2.get("status") in TERMINAL
                elif r2 is not None and (r2.status_code == 429 or 400 <= r2.status_code < 500):
                    rest_ok = True        # refused: nothing more was sent
                how += f", rest at market after {self.MID_WAIT_S:g} s"
                if not rest_ok:
                    uncertain_rest = True
        if oid and final.get("status") not in TERMINAL and not (price and "rest at market" in how):
            # Still working (a market order that did not report done): cancel what is left so the
            # size is known before protection is rebuilt.
            try:
                self._logged("cancel_order", lambda: client.cancel_order(self._hash, oid))
            except Exception:
                pass
            final = self._wait_fill(client, oid, 1.5)
            fills = self._fills(final) or fills
            filled = sum(q for q, _ in fills)
        if not oid or final.get("status") not in TERMINAL or uncertain_rest:
            avg = sum(q * p for q, p in fills) / filled if filled else None
            self.store.log_action("partial", symbol, oid, "UNKNOWN", f"{instr} {filled:g}/{n} ({how}), not final")
            return {"filled": filled, "avg": avg, "how": how, "unresolved": True}
        avg = sum(q * p for q, p in fills) / filled if filled else None
        self.store.log_action("partial", symbol, oid, final.get("status", "SENT"),
                              f"{instr} {filled:g}/{n} ({how})" + (f" @ {avg:.4f}" if avg else ""))
        return {"filled": filled, "avg": avg, "how": how}

    def _partial_locked(self, symbol: str, pct, shares, orders: list[dict], known_terminal: set[int],
                        mode: str = "mid", prior_trail_level=None, confirm_leg: bool = False) -> dict:
        from .orders import pending_orders
        client = self._ensure()
        blocked = self._blocked(symbol, client)
        if blocked:
            return blocked
        positions = self._positions(client)
        qty, asset = self._position(client, symbol, positions)
        if abs(qty) < 1e-9:
            return {"ok": False, "symbol": symbol, "error": f"No open {symbol} position."}
        side = "LONG" if qty > 0 else "SHORT"
        size = abs(qty)
        unit_w, _ = _units(asset)
        one = unit_w[:-1]
        if shares:
            n = int(float(shares))
        else:
            n = int(size * float(pct or 0))
        if n < 1:
            return {"ok": False, "symbol": symbol, "error": f"{pct or shares} of {size:g} {unit_w} is less than 1 {one}."}
        if n >= size:
            return self._close_locked(symbol, orders, known_terminal, confirm_leg)
        instr = {("EQUITY", True): "SELL", ("EQUITY", False): "BUY_TO_COVER",
                 ("OPTION", True): "SELL_TO_CLOSE", ("OPTION", False): "BUY_TO_CLOSE"}.get((asset, side == "LONG"))
        if not instr:
            return {"ok": False, "symbol": symbol, "error": f"Cannot take a partial on a {asset} position here."}
        fresh = self._fresh_orders(client, orders)
        if not confirm_leg:
            guard = self._guard_leg(symbol, qty, asset, positions, fresh, known_terminal)
            if guard:
                return guard
        terminal = terminal_ids(fresh) | known_terminal
        pend = pending_orders(fresh, terminal)
        others = [p for p in pend if p.symbol == symbol and p.live and p.role not in ("STOP", "TARGET")]
        if others:
            return {"ok": False, "symbol": symbol,
                    "error": f"{symbol} has other working orders ({len(others)}) that Schwab would cancel. "
                             "Take this partial in thinkorswim."}
        stop, target = self._exit_plan(pend, symbol, side, n)
        trail = self._trail_of(pend, symbol, side)
        sent = self._send_partial(client, symbol, instr, asset, n, mode)
        if sent.get("error"):
            return {"ok": False, "symbol": symbol, "error": sent["error"], "unresolved_close": bool(sent.get("unresolved"))}
        filled, avg, how = sent["filled"], sent["avg"], sent["how"]
        head = (f"Took {filled:g} of {size:g} {symbol}" + (f" @ {avg:.2f}" if avg else "") + f" ({how})."
                if filled else f"Partial sent ({how}), not filled yet.")
        # Rebuild protection on what is left, at the same levels. Schwab's position can lag the
        # fill: wait for it to show the expected size, never protect more than is surely there,
        # and never send exits for the old side if the position flipped.
        expected = size - filled
        left, _ = self._position(client, symbol)
        # Wait for Schwab's position to show a confirmed fill, but not when the partial's outcome is
        # unconfirmed (one read is enough then: protection is capped below) or during a 429 pause.
        for _ in range(0 if sent.get("unresolved") else 6):
            if abs(left) < 1e-9 or abs(abs(left) - expected) < 1e-9:
                break
            if self.cooldown_left and self.cooldown_left() > 0:
                break
            time.sleep(0.4)
            left, _ = self._position(client, symbol)
        if abs(left) < 1e-9:
            return {"ok": True, "symbol": symbol, "message": head + " Position is now flat."}
        if (left > 0) != (side == "LONG"):
            msg = (head + f" WARNING: the position is now {'LONG' if left > 0 else 'SHORT'} {abs(left):g}: "
                   "no exits were sent. Check thinkorswim now.")
            self.store.log_action("partial", symbol, None, "UNKNOWN", msg)
            return {"ok": False, "symbol": symbol, "error": msg, "unresolved_close": True}
        size_note = ""
        if abs(abs(left) - expected) > 1e-9:
            size_note = (f" CHECK: Schwab shows {abs(left):g} {unit_w}, expected {expected:g}; protecting "
                         f"{min(abs(left), expected):g}.")
        if sent.get("unresolved"):
            # The partial may still fill: protect only what is left even if all of it fills, so the
            # new exits can never be larger than the position.
            expected = size - n
            size_note += (f" The partial is not confirmed final: protecting {min(abs(left), expected):g} {unit_w} (what "
                          "remains even if it fully fills). Check thinkorswim, then press Resolve.")
        if expected < 1e-9:
            return {"ok": False, "symbol": symbol, "unresolved_close": True,
                    "error": head + size_note + " No exits sent."}
        left = min(abs(left), expected)
        head += size_note
        if trail:
            res = self._create_stop(client, symbol, side, left, asset, None, [], set(),
                                    target=Decimal(str(target)) if target is not None else None, rebuild=True,
                                    trail=trail)
            msg = head + " " + (res.get("message") or res.get("error") or "") + self._restart_note(
                symbol, side, trail, prior_trail_level)
            if not res.get("ok"):
                msg += f" CHECK: the {abs(left):g} remaining {unit_w} may have no stop. Use Move stop or thinkorswim."
            return {"ok": bool(res.get("ok")) and filled >= n - 1e-9, "symbol": symbol, "filled": filled, "avg": avg,
                    "message": msg, "error": None if res.get("ok") and filled >= n - 1e-9 else msg}
        if stop is None and target is None:
            return {"ok": filled > 0, "symbol": symbol,
                    "message": head + f" {abs(left):g} {unit_w} left; there was no stop or target to put back."}
        if stop is None:
            # A target-only position: Schwab dropped the target when the partial entered, so put it
            # back on what is left (a lone LIMIT, as Set target does without a stop).
            nleft = int(left) if float(left).is_integer() else left
            tk = ticks_for(asset)
            self.store.log_action("partial", symbol, None, "SENDING", f"target {fmt(Decimal(str(target)), tk)}, no stop")
            try:
                r = self._place_exit(client, symbol, instr, asset, nleft, Decimal(str(target)))
                ok = r.status_code in (200, 201)
                if ok:
                    tgt_txt = f" Target {fmt(Decimal(str(target)), tk)} put back on the {abs(left):g} {unit_w} left."
                elif 400 <= r.status_code < 500:
                    tgt_txt = (f" Schwab refused the target (HTTP {r.status_code}): {r.text[:150]}."
                               " The target is GONE: use Set target.")
                else:                             # a 5xx may still have placed it
                    tgt_txt = (f" Schwab's answer on the target was unclear (HTTP {r.status_code}): it MAY be "
                               "working. Check thinkorswim before using Set target.")
            except Exception as e:
                ok = False
                tgt_txt = (f" No answer from Schwab on the target ({type(e).__name__}): it MAY be working. "
                           "Check thinkorswim.")
            self.store.log_action("partial", symbol, None, "DONE" if ok else "CHECK", tgt_txt.strip())
            return {"ok": False, "symbol": symbol,
                    "error": head + tgt_txt + f" {abs(left):g} {unit_w} left with NO STOP (there was none before). "
                                              "Use Stop to B/E or Move stop.",
                    "unresolved_close": bool(sent.get("unresolved"))}
        res = self._create_stop(client, symbol, side, left, asset, Decimal(str(stop)), [], set(),
                                target=Decimal(str(target)) if target is not None else None, rebuild=True)
        msg = head + " " + (res.get("message") or res.get("error") or "")
        if not res.get("ok"):
            msg += f" CHECK: the {abs(left):g} remaining {unit_w} may have no stop. Use Move stop or thinkorswim."
        return {"ok": bool(res.get("ok")) and filled >= n - 1e-9, "symbol": symbol, "filled": filled, "avg": avg,
                "message": msg, "error": None if res.get("ok") and filled >= n - 1e-9 else msg,
                "unresolved_close": bool(sent.get("unresolved"))}

    def _create_stop(self, client, symbol: str, side: str, qty: float, asset: str | None,
                     price: Decimal | None, fresh: list[dict], terminal: set[int],
                     target: Decimal | None = None, rebuild: bool = False, trail: dict | None = None,
                     preview: bool = False) -> dict:
        """Create a stop for the whole position. Schwab cancels an instrument's working orders
        when a new order is entered, so an existing single target is re-sent together with the
        new stop as one OCO (the target survives and gets linked).

        rebuild=True (after a partial): Schwab already cancelled the old exits; send exactly
        `target` (if any) + stop for `qty`, without looking at what is working.

        preview=True asks Schwab's order preview first and sends nothing it refuses (the result
        then has not_sent=True). Only for a change that keeps the current exits working until the
        new order enters; never after a partial, where Schwab has already dropped them."""
        from .orders import pending_orders
        instr = {("EQUITY", True): "SELL", ("EQUITY", False): "BUY_TO_COVER",
                 ("OPTION", True): "SELL_TO_CLOSE", ("OPTION", False): "BUY_TO_CLOSE"}.get((asset, side == "LONG"))
        if not instr:
            return {"ok": False, "symbol": symbol, "error": f"Cannot set a stop on a {asset} position here."}
        size = abs(qty)
        n = int(size) if float(size).is_integer() else size
        leg = [{"instruction": instr, "quantity": n, "instrument": {"symbol": symbol, "assetType": asset}}]
        from .bracket import stop_leg
        tk = ticks_for(asset)              # formatting only: the prices are already on their grid
        unit_w, us = _units(asset)
        stop = stop_leg(trail or price, leg, ticks=tk)
        stop_txt = _trail_txt(trail) if trail else f"stop {fmt(price, tk)}"
        targets = []
        if not rebuild:
            targets = [p for p in pending_orders(fresh, terminal)
                       if p.symbol == symbol and p.role == "TARGET" and p.live and p.exits(side)]
            others = [p for p in pending_orders(fresh, terminal) if p.symbol == symbol and p.live
                      and p.role != "TARGET"]
            if others:
                return {"ok": False, "symbol": symbol,
                        "error": f"{symbol} has other working orders ({len(others)}); a new stop would make Schwab "
                                 "cancel them. Set this stop in thinkorswim."}
            if len(targets) > 1 or (targets and abs(targets[0].qty - size) > 1e-9) or (targets and targets[0].legs != 1):
                return {"ok": False, "symbol": symbol,
                        "error": f"{symbol} has several or partial targets; a new stop would make Schwab cancel "
                                 "them. Set this stop in thinkorswim."}
            if targets:
                target = Decimal(str(targets[0].price))
        if target is not None:
            order = {"orderStrategyType": "OCO", "childOrderStrategies": [
                {"orderStrategyType": "SINGLE", "orderType": "LIMIT", "session": "NORMAL",
                 "duration": "DAY", "price": fmt(target, tk), "orderLegCollection": leg},
                stop]}
            what = f"{stop_txt} + target {fmt(target, tk)} as one OCO"
            targets = targets or [True]
        else:
            order, what = stop, stop_txt
        if preview:
            refused = self._preview_refusal(client, order)
            if refused:
                rejected, why = refused
                what_txt = "trailing stop" if trail else "order"
                head = (f"Schwab does not accept this {what_txt} on {symbol}: {why}" if rejected else
                        f"Schwab's preview did not confirm this {what_txt} on {symbol} ({why})")
                return {"ok": False, "symbol": symbol, "not_sent": True,
                        "error": head.rstrip(".") + ". Nothing was sent; your current stop and target are still working."}
        self.store.log_action("create_stop", symbol, None, "SENDING", f"{what}, {n} {us}")
        log.warning("CREATE STOP %s %s %s", symbol, n, what)
        try:
            r = self._logged("place_order", lambda: client.place_order(self._hash, order))
        except Exception as e:
            self.store.log_action("create_stop", symbol, None, "UNKNOWN", repr(e))
            return {"ok": False, "symbol": symbol,
                    "error": "No answer from Schwab: the stop MAY have been placed. Check thinkorswim."}
        if r.status_code not in (200, 201):
            self.store.log_action("create_stop", symbol, None, "REJECTED", f"HTTP {r.status_code} {r.text[:200]}")
            return {"ok": False, "symbol": symbol,
                    "error": f"Schwab refused the stop (HTTP {r.status_code}): {r.text[:150]}. "
                             + ("Your target is still working." if targets else "The position has no stop.")}
        tail = r.headers.get("Location", "").rstrip("/").split("/")[-1]
        new_id = int(tail) if tail.isdigit() else None
        # Auto B/E follows a stop the companion rebuilt itself (never a trailing one). Linked as
        # soon as Schwab accepts it, so a slow read below cannot lose it; auto_breakeven still
        # checks the stop is live, fixed and not loosened before it moves anything.
        linked = False
        if new_id is not None and not trail:
            since = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
            linked = bool(self.store.link_auto_be(symbol, new_id, since.isoformat()))
            if price is not None:
                self.store.record_stop(symbol, price, new_id)   # r_basis checks it worked before using it
        time.sleep(0.6)
        after = self._fresh_orders(client)
        # Only the order we just sent counts (a delayed read can still show the previous exits).
        live = [p for p in pending_orders(after, terminal_ids(after)) if p.symbol == symbol and p.live
                and p.exits(side) and (new_id is None or p.group_id == new_id)]

        def same_trail(p) -> bool:
            return (p.order_type.startswith("TRAILING") and p.trail_offset is not None
                    and abs(Decimal(str(p.trail_offset)) - trail["offset"]) < Decimal("0.00001")
                    and (p.trail_link or "VALUE") == trail["link"] and (p.trail_basis or "MARK") == trail["basis"])
        new_stops = ([p for p in live if p.role == "STOP" and same_trail(p)] if trail else
                     [p for p in live if p.role == "STOP" and p.stop is not None
                      and abs(Decimal(str(p.stop)) - price) < Decimal("0.00001")])
        if new_id is None:            # without the new order's id an old order could look like it
            new_stops, live = [], []
        covered = sum(p.qty for p in new_stops)
        tgt_ok = not targets or any(p.role == "TARGET" and p.oco_id is not None
                                    and any(s.oco_id == p.oco_id for s in new_stops) for p in live)
        ok = covered >= size - 1e-9 and tgt_ok
        msg = ((f"Trailing stop ({_trail_txt(trail)}) working" if trail else f"Stop created at {fmt(price, tk)}")
               + f" for {covered:g} of {size:g} {unit_w}"
               + (", linked with the target in one OCO." if targets and tgt_ok else ".")
               + ("" if tgt_ok else " WARNING: the target is not working now: check thinkorswim.")
               + ("" if covered >= size - 1e-9 else " Not confirmed yet: check thinkorswim."))
        # A target at or through the market fills as it enters: the new OCO is then already done
        # (target filled, stop cancelled by the OCO), which is a close, not a missing stop.
        tgt_fill = self._filled_target(after, new_id) if new_id is not None and target is not None and not ok else None
        note, closed = "", False
        if tgt_fill:
            filled, avg = tgt_fill
            fill_txt = f"The target {fmt(target, tk)} filled {filled:g} {unit_w}" + (f" @ {avg:g}" if avg else "") + " as it entered"
            if filled >= size - 1e-9:
                # The exit size can be less than the position (a partial whose outcome is open):
                # only a fresh flat position is a close.
                try:
                    left, _ = self._position(client, symbol)
                except Exception:
                    left = None
                if left is not None and abs(left) < 1e-9:
                    ok = closed = True
                    msg = fill_txt + ": position closed."
                else:
                    msg = fill_txt + (f", but {abs(left):g} {unit_w} are still open" if left is not None
                                      else ", and the position could not be read") + ": check their stop in thinkorswim."
            else:
                msg = fill_txt + f" ({filled:g} of {size:g}). Check the stop on the rest in thinkorswim."
        elif linked:
            note = " Auto B/E stays on this stop."
            msg += note
        self.store.log_action("create_stop", symbol, None, "DONE" if ok else "CHECK", msg)
        return {"ok": ok, "symbol": symbol, "stop": fmt(price, tk) if price is not None else None, "created": True,
                "trail": bool(trail), "message": msg, "note": note, "closed": closed,
                "error": None if ok else msg}

    def _filled_target(self, orders: list[dict], oco_id: int) -> tuple[float, float | None] | None:
        """(filled quantity, average price) of the LIMIT target inside the OCO just sent, or None."""
        tree = self._find_in(orders, oco_id)
        for ch in (tree or {}).get("childOrderStrategies") or []:
            if ch.get("orderType") != "LIMIT":
                continue
            filled = float(ch.get("filledQuantity") or 0)
            if filled <= 0:
                return None
            legs = [e for a in ch.get("orderActivityCollection") or [] if a.get("executionType") == "FILL"
                    for e in a.get("executionLegs") or []]
            q = sum(float(e.get("quantity") or 0) for e in legs)
            avg = sum(float(e["price"]) * float(e["quantity"]) for e in legs) / q if q else None
            return filled, avg
        return None

    def _preview_refusal(self, client, order: dict) -> tuple[bool, str] | None:
        """Ask Schwab's order preview about `order`. None = Schwab would accept it; otherwise
        (rejected, reason): rejected=True when Schwab said no, False when the preview gave no clear
        answer (no reply, a rate limit, a server error, an unreadable body). Either way nothing is
        sent: when unsure, refuse."""
        try:
            r = self._logged("preview_order", lambda: client.preview_order(self._hash, order))
        except Exception as e:
            return False, f"no answer: {type(e).__name__}"
        if isinstance(r, _Throttled) or r.status_code == 429:
            return False, "rate limit; try again in a few seconds"
        if 400 <= r.status_code < 500:
            return True, f"HTTP {r.status_code} {r.text[:200]}".strip()
        if r.status_code not in (200, 201):
            return False, f"HTTP {r.status_code}"
        try:
            body = r.json() or {}
        except Exception:
            return False, "its answer could not be read"
        # Only a body shaped like Schwab's preview counts as an answer: {}, null, a list or an
        # unrelated object is unclear, never "no rejects".
        if not isinstance(body, dict):
            return False, "its answer was not a preview"
        result = body.get("orderValidationResult")
        if not isinstance(result, dict) and not isinstance(body.get("orderStrategy"), dict):
            return False, "its answer was not a preview"
        rejects = result.get("rejects", []) if isinstance(result, dict) else []
        if not isinstance(rejects, list):         # "rejects": null or a string is unreadable, not "none"
            return False, "its answer could not be read"
        if rejects:
            msgs = [str(x.get("message") or x.get("activityMessage") or x.get("validationRuleName") or "")
                    for x in rejects if isinstance(x, dict)]
            return True, "; ".join(m for m in msgs if m)[:300] or "the preview lists a rejection"
        return None

    def resolve(self, ticket_id: str) -> None:
        """the user checked thinkorswim; unblock new submissions."""
        self.store.update_ticket(ticket_id, "RESOLVED_MANUALLY", detail="resolved by the user")

    # ---- reconciliation (called by the poller with fresh orders) ------------

    @staticmethod
    def _shape(o: dict):
        """Comparable shape of a bracket: entry (instruction, symbol, qty, limit) and each exit
        OCO as (qty, instruction, target, stop). None if it is not a TRIGGER -> OCO(s) bracket."""
        def num(v):
            try:
                return round(float(v), 4)
            except (TypeError, ValueError):
                return None

        def stop_of(k):
            if k.get("orderType") in ("STOP", "STOP_LIMIT"):
                return num(k.get("stopPrice"))
            if k.get("orderType") == "TRAILING_STOP":
                return ("TRAIL", k.get("stopPriceLinkType"), k.get("stopPriceLinkBasis"), num(k.get("stopPriceOffset")))
            return None

        legs = o.get("orderLegCollection") or []
        if o.get("orderStrategyType") not in ("TRIGGER", "SINGLE") or len(legs) != 1:
            return None
        entry = (legs[0].get("instruction"), (legs[0].get("instrument") or {}).get("symbol"),
                 num(legs[0].get("quantity") or o.get("quantity")), num(o.get("price")))
        if o.get("orderStrategyType") == "SINGLE":
            # An entry sent without stop or target: only an opening order can be a ticket's entry.
            return ((entry, ()) if not o.get("childOrderStrategies") and entry[0] in OPENING_INSTRUCTIONS | {"BUY"}
                    else None)
        exits = []
        for oco in o.get("childOrderStrategies") or []:
            kids = oco.get("childOrderStrategies") or []
            if oco.get("orderStrategyType") == "SINGLE":
                # A stop alone or a target alone under the entry.
                leg = (oco.get("orderLegCollection") or [{}])[0]
                q = num(oco.get("quantity") or leg.get("quantity"))
                if oco.get("orderType") == "LIMIT":
                    exits.append((q, leg.get("instruction"), num(oco.get("price")), None))
                elif stop_of(oco) is not None:
                    exits.append((q, leg.get("instruction"), None, stop_of(oco)))
                else:
                    return None
                continue
            if oco.get("orderStrategyType") != "OCO" or len(kids) != 2:
                return None
            kl = [(k.get("orderLegCollection") or [{}])[0] for k in kids]
            if len({(leg.get("instruction"), (leg.get("instrument") or {}).get("symbol")) for leg in kl}) != 1:
                return None
            qtys = {num(k.get("quantity") or leg.get("quantity")) for k, leg in zip(kids, kl)}
            stops = [num(k.get("stopPrice")) for k in kids if k.get("orderType") == "STOP"] + \
                    [("TRAIL", k.get("stopPriceLinkType"), k.get("stopPriceLinkBasis"), num(k.get("stopPriceOffset")))
                     for k in kids if k.get("orderType") == "TRAILING_STOP"]
            targets = [num(k.get("price")) for k in kids if k.get("orderType") == "LIMIT"]
            if len(qtys) != 1 or len(stops) != 1 or len(targets) != 1:
                return None
            exits.append((qtys.pop(), kl[0].get("instruction"), targets[0], stops[0]))
        return (entry, tuple(sorted(exits, key=repr))) if exits else None

    @classmethod
    def _matches(cls, o: dict, t: dict) -> bool:
        """The whole bracket Schwab reports must equal the order this ticket sent."""
        sent = t.get("request")
        if not sent:
            return False
        a, b = cls._shape(o), cls._shape(sent)
        return a is not None and a == b

    def reconcile(self, orders_today: list[dict]) -> None:
        """Attach a Schwab order id to tickets sent without one (UNKNOWN / untracked).

        Only a unique, unclaimed order whose full bracket matches, entered within 2 minutes
        after the ticket, counts. Anything ambiguous stays unresolved for the user to check.
        """
        since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        claimed = self.store.claimed_order_ids()
        for t in self.store.tickets_since(since):
            if t["status"] not in ("UNKNOWN", "SENDING", "ACCEPTED_UNTRACKED"):
                continue
            created = datetime.fromisoformat(t["created"])
            lo, hi = created - timedelta(seconds=5), created + timedelta(minutes=2)
            cands = [o for o in orders_today
                     if o.get("orderId") not in claimed and self._matches(o, t)
                     and lo <= datetime.strptime(o["enteredTime"], "%Y-%m-%dT%H:%M:%S%z") <= hi]
            if len(cands) == 1:
                oid = cands[0]["orderId"]
                self.store.update_ticket(t["ticket_id"], "ACCEPTED", order_id=oid,
                                         detail="matched to Schwab order by the poller")
                claimed.add(oid)

    def brackets(self, orders_today: list[dict], pend: list[PendingOrder],
                 positions: dict[str, float], orders_fresh: bool) -> list[dict]:
        """Today's tickets. The latest ticket per symbol shows live protection, measured on the
        whole position with every live stop and target (so ToS edits and replacements count)."""
        by_id = {o["orderId"]: o for o in orders_today}
        start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        out, seen = [], set()
        from .view import pretty_symbol
        for t in self.store.tickets_since(start.isoformat()):   # newest first
            row = dict(t)
            legs = (t.get("request") or {}).get("orderLegCollection") or [{}]
            row["asset_type"] = (legs[0].get("instrument") or {}).get("assetType") or "EQUITY"
            row["display"] = pretty_symbol(t["symbol"], row["asset_type"])
            tree = by_id.get(t["order_id"]) if t["order_id"] else None
            # Only a filled ticket claims the symbol, so a newer rejected or unfilled ticket
            # never hides the protection status of a position an earlier ticket opened.
            filled_now = bool(tree) and float(tree.get("filledQuantity") or 0) > 0
            latest = t["symbol"] not in seen
            if filled_now:
                seen.add(t["symbol"])
            if t["status"] in ("UNKNOWN", "SENDING", "ACCEPTED_UNTRACKED"):
                row["protection"] = "UNKNOWN"
            elif t["status"] != "ACCEPTED":
                row["protection"] = t["status"]
            elif tree is None:
                row["protection"] = "CHECKING"
            else:
                filled = float(tree.get("filledQuantity") or 0)
                fill_times = [e.get("time") for a in tree.get("orderActivityCollection") or []
                              if a.get("executionType") == "FILL" for e in a.get("executionLegs") or []
                              if e.get("time")]
                row.update({"entry_status": tree.get("status"), "filled": filled,
                            "first_fill": min(fill_times) if fill_times else None})
                if tree.get("status") == "REJECTED" and tree.get("statusDescription"):
                    row["entry_reason"] = str(tree["statusDescription"])[:200]   # accepted, then refused
                direction = 1 if t["side"] == "LONG" else -1
                exposure = max(0.0, positions.get(t["symbol"], 0.0) * direction)
                if filled == 0:
                    row["protection"] = "WAITING_FILL" if tree.get("status") in LIVE else "NOT_FILLED"
                elif not latest:
                    row["protection"] = "EARLIER"
                elif not orders_fresh:
                    row["protection"] = "UNKNOWN"   # stale data proves neither open nor closed
                elif exposure <= 0:
                    row["protection"] = "CLOSED"
                else:
                    covered, _ = stop_coverage(pend, t["symbol"], t["side"], exposure)
                    tgt = sum(o.qty for o in pend if o.symbol == t["symbol"] and o.role == "TARGET"
                              and o.live and o.legs == 1 and o.exits(t["side"]))
                    row.update({"exposure": exposure, "stop_qty": covered,
                                "target_qty": min(tgt, exposure)})
                    row["protection"] = ("NO_STOP" if covered < 1e-9 else
                                         "PARTIAL" if covered + 1e-9 < exposure else "PROTECTED")
            out.append(row)
        return out

    def r_basis(self, orders_today: list[dict]) -> dict[str, dict]:
        """Frozen R per symbol from the latest filled companion ticket today: the entry's average
        fill and the ORIGINAL stop. Later stop moves or adds never redefine R. An entry edited in
        thinkorswim before it filled is followed to the order that replaced it. Older filled
        tickets on the symbol ride along in "alts" (newest first) so the poller can pick the one
        that opened the current position."""
        by_id = {o["orderId"]: o for o in orders_today}
        nodes: dict[int, dict] = {}

        def walk(o):
            if o.get("orderId") is not None:
                nodes.setdefault(o["orderId"], o)
            for c in o.get("childOrderStrategies") or []:
                walk(c)
        for o in orders_today:
            walk(o)

        def stop_state(oid) -> str:
            # A recorded stop (a single stop or the OCO it went in) counts once its stop leg has
            # worked; REJECTED anywhere in it skips it; anything else means wait (no R yet).
            node, sub = nodes.get(oid), []
            stack = [node] if node else []
            while stack:
                n = stack.pop()
                sub.append(n)
                stack.extend(n.get("childOrderStrategies") or [])
            if any(n.get("status") == "REJECTED" for n in sub):
                return "rejected"
            legs = [n for n in sub if n.get("orderType") in ("STOP", "STOP_LIMIT")]
            return "ok" if legs and legs[0].get("status") in WORKED else "wait"
        start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        cands: dict[str, list[dict]] = {}
        for t in self.store.tickets_since(start.isoformat()):   # newest first
            if t["status"] != "ACCEPTED" or not t["order_id"]:
                continue
            tree = by_id.get(t["order_id"])
            for _ in range(10):
                if not tree or tree.get("status") != "REPLACED":
                    break
                tree = _replacement(tree, orders_today)
            legs = [e for a in (tree or {}).get("orderActivityCollection") or []
                    if a.get("executionType") == "FILL" for e in a.get("executionLegs") or []]
            qty = sum(float(e.get("quantity") or 0) for e in legs)
            if qty <= 0:
                continue
            entry = sum(float(e["price"]) * float(e["quantity"]) for e in legs) / qty
            first_fill = min((e.get("time") for e in legs if e.get("time")), default=None)
            stop_txt = t["stop"] if _dec(t["stop"]) else self.store.frozen_stop(t["ticket_id"])
            if stop_txt is None and first_fill:
                # A ticket sent without a stop takes its R from the first fixed stop the companion
                # set and saw working after the fill (Set stop), on the risk side of the entry.
                # Fill times have whole seconds: only stops from the next second on count, so a
                # stop of the trade before (same-second re-entry) is never borrowed.
                try:
                    since = (datetime.strptime(first_fill, "%Y-%m-%dT%H:%M:%S%z")
                             + timedelta(seconds=1)).astimezone(timezone.utc)
                except ValueError:
                    since = None
                first = None
                for st, oid in self.store.stops_since(t["symbol"], since.isoformat()) if since else []:
                    state = stop_state(oid)
                    if state != "rejected":
                        first = st if state == "ok" else None   # a later stop never jumps an unconfirmed one
                        break
                if first and _dec(first) and (float(first) < entry if t["side"] == "LONG" else float(first) > entry):
                    # Frozen on the ticket the first time it qualifies: later rejections of other
                    # legs, status changes or a missing listing never take it back or replace it.
                    stop_txt = self.store.freeze_stop(t["ticket_id"], first)
            if stop_txt is None:
                continue                  # no stop price yet: no R
            stop = float(stop_txt)
            rps = abs(entry - stop)
            if rps > 0:
                cands.setdefault(t["symbol"], []).append(
                    {"side": t["side"], "entry": entry, "stop": stop, "rps": rps,
                     "ticket_id": t["ticket_id"], "first_fill": first_fill})
        return {sym: {**c[0], "alts": c[1:]} for sym, c in cands.items()}

    def status(self) -> dict:
        return {"armed": self.armed, "market_open": market_open(),
                "blocked": bool(self.store.open_tickets()), "risk_choices": RISK_CHOICES}
