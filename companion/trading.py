"""The order ticket service: one click = one Schwab bracket (entry + stop + target).

Safety that is engineering, not coaching:
- Off until the user arms it in the dashboard (persisted). It never places orders on its own.
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
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_FLOOR, Decimal

from . import schwab
from .bracket import MIN_STOP_TICKS, Plan, bracket_order, fmt, plan, tick
from .orders import LIVE, TERMINAL, PendingOrder, stop_coverage, terminal_ids
from .pnl import ET
from .store import Store, now_iso

log = logging.getLogger("companion.trading")

LOCK_R = Decimal("0.1")       # the click keeps the displayed plan while price moved <= 0.1R against it
LOCK_MIN_TICKS = 2            # ...and at least 2 ticks (2 cents above $1)
RISK_CHOICES = (250, 375, 500, 750)      # defaults; the user edits them with the gear on the ticket


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
    dist = f"{off:g}%" if spec["link"] == "PERCENT" else f"${fmt(Decimal(str(off)))}"
    return f"trailing {dist} from {spec['basis'].lower()}"


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
                if endpoint in ("order_details", "ticket_quote"):
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
        out = {"symbol": sym, "found": bool(q), "bid": q.get("bidPrice"), "ask": q.get("askPrice"),
               "last": q.get("lastPrice"), "change_pct": q.get("netPercentChange"),
               "name": ref.get("description"), "exchange": ref.get("exchangeName"),
               "asset_type": entry.get("assetMainType")}
        self._quote_cache[sym] = (time.monotonic(), out)
        return out

    def make_plan(self, req: dict, fresh: bool = False, q: dict | None = None, limit_override=None) -> Plan:
        q = q or self.quote(req["symbol"], max_age=0 if fresh else 1.0)
        return plan(req["symbol"], req["side"], req["stop"], req["risk"], req.get("r", 2),
                    q.get("bid"), q.get("ask"), split=req.get("split") or 0, t1_r=req.get("t1_r") or 1,
                    entry=req.get("entry") or "cross", target_price=req.get("target_price"),
                    t1_price=req.get("t1_price"), stop_type=req.get("stop_type") or "fixed",
                    trail_unit=req.get("trail_unit") or "R", trail_value=req.get("trail_value") or 1,
                    trail_basis=req.get("trail_basis") or "MARK", limit_override=limit_override)

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
        if not (exp_limit.is_finite() and exp_target.is_finite()) or exp_limit <= 0 or exp_target <= 0 or exp_qty <= 0:
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
        lock = max(LOCK_R * p.per_share, LOCK_MIN_TICKS * tick(exp_limit))
        long = p.side == "LONG"
        against = now.limit - exp_limit if long else exp_limit - now.limit
        if against > lock:
            return {"ok": False, "error": f"Price moved: entry is now {fmt(now.limit)} (ticket showed "
                    f"{fmt(exp_limit)}, more than {fmt(lock)} away). Review and click again.",
                    "plan": now.as_dict()}
        # After a move in favour the fill comes nearer the stop: keep the minimum stop distance
        # from where it would fill now, not from the displayed price.
        room = now.limit - p.stop if long else p.stop - now.limit
        if room < MIN_STOP_TICKS * tick(now.limit):
            return {"ok": False, "error": f"Price is within {MIN_STOP_TICKS} ticks of the stop {fmt(p.stop)}. "
                    "Review and click again.", "plan": now.as_dict()}
        if p.qty < exp_qty:
            return {"ok": False, "error": f"Size changed to {p.qty} shares (ticket showed {exp_qty}). "
                    "Review and click again.", "plan": now.as_dict()}
        # Never more shares than displayed; recompute the numbers for that size.
        p.qty = exp_qty
        p.planned_risk = p.per_share * p.qty
        p.notional = p.limit * p.qty
        # Same entry price and inputs give the same targets; a difference means the ticket's
        # inputs changed after it was displayed.
        if p.target != exp_target:
            return {"ok": False, "error": f"Target is now {fmt(p.target)} (ticket showed {fmt(exp_target)}). "
                    "Review and click again.", "plan": now.as_dict()}
        if p.two_targets:
            p.qty1 = int((p.qty * p.split).to_integral_value(rounding=ROUND_FLOOR))
            exp_qty1 = int(req.get("expected_qty1") or 0)
            exp_t1 = Decimal(str(req.get("expected_target1") or 0))
            if p.qty1 != exp_qty1 or p.qty1 < 1 or p.qty - p.qty1 < 1:
                return {"ok": False, "error": f"Split changed to {p.qty1}/{p.qty - p.qty1} shares. "
                        "Review and click again.", "plan": now.as_dict()}
            if p.target1 != exp_t1:
                return {"ok": False, "error": f"Target 1 is now {fmt(p.target1)}. Review and click again.",
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
                return {"ok": False, "error": f"The trail would now start at {fmt(p.trail_start)} (ticket showed "
                        f"{fmt(exp_start)}). Review and click again.", "plan": now.as_dict()}
        elif req.get("expected_trail_offset"):
            return {"ok": False, "error": "Ticket showed a trailing stop but the plan has a fixed one. Review.",
                    "plan": p.as_dict()}
        order = bracket_order(p)
        intent = {"ticket_id": ticket_id, "symbol": p.symbol, "side": p.side, "qty": p.qty,
                  "limit": fmt(p.limit), "stop": fmt(p.stop), "target": fmt(p.target),
                  "risk": float(p.planned_risk), "request": order}
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

    def _position(self, client, symbol: str) -> tuple[float, str | None]:
        r = self._logged("account_details", lambda: client.account_details(self._hash, fields="positions"))
        r.raise_for_status()
        for p in ((r.json() or {}).get("securitiesAccount") or {}).get("positions") or []:
            ins = p.get("instrument") or {}
            if ins.get("symbol") == symbol:
                return (float(p.get("longQuantity") or 0) - float(p.get("shortQuantity") or 0),
                        ins.get("assetType"))
        return 0.0, None

    def close_position(self, symbol: str, orders: list[dict], known_terminal: set[int]) -> dict:
        """One market order for the whole position. Schwab cancels the symbol's other working
        orders (stop, target) itself when it enters (previewOrder warning, 2026-10-22), the way
        thinkorswim's Flatten works; the companion checks afterwards and cleans up anything left."""
        symbol = symbol.upper().strip()
        if not market_open():
            return {"ok": False, "symbol": symbol, "error": "Regular hours only (9:30 to 16:00 ET)."}
        if not self._send_lock.acquire(blocking=False):
            return {"ok": False, "symbol": symbol, "error": "Another order action is in progress."}
        try:
            return self._close_locked(symbol, orders, known_terminal)
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

    def _close_locked(self, symbol: str, orders: list[dict], known_terminal: set[int]) -> dict:
        client = self._ensure()
        blocked = self._blocked(symbol, client)
        if blocked:
            return blocked
        qty, asset = self._position(client, symbol)
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

    def _move_stop_locked(self, symbol: str, new_stop, orders: list[dict], known_terminal: set[int]) -> dict:
        from .bracket import _to_tick
        from decimal import ROUND_HALF_UP
        client = self._ensure()
        blocked = self._blocked(symbol, client)
        if blocked:
            return blocked
        qty, asset = self._position(client, symbol)
        if abs(qty) < 1e-9:
            return {"ok": False, "symbol": symbol, "error": f"No open {symbol} position."}
        side = "LONG" if qty > 0 else "SHORT"
        price = Decimal(str(new_stop))
        price = _to_tick(price, ROUND_HALF_UP)
        q = self.quote(symbol, max_age=0)
        bid, ask = Decimal(str(q.get("bid") or 0)), Decimal(str(q.get("ask") or 0))
        if side == "LONG" and bid and price >= bid:
            return {"ok": False, "symbol": symbol,
                    "error": f"Stop {fmt(price)} is at or above the bid {fmt(bid)}: it would trigger now. Not moved."}
        if side == "SHORT" and ask and price <= ask:
            return {"ok": False, "symbol": symbol,
                    "error": f"Stop {fmt(price)} is at or below the ask {fmt(ask)}: it would trigger now. Not moved."}
        fresh = self._fresh_orders(client, orders)
        terminal = terminal_ids(fresh) | known_terminal
        from .orders import pending_orders
        stops = [p for p in pending_orders(fresh, terminal)
                 if p.symbol == symbol and p.role == "STOP" and p.live and p.legs == 1 and p.exits(side)]
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
            self.store.log_action("move_stop", symbol, None, "SENDING", f"trailing -> fixed {fmt(price)}")
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
            legs = cur.get("orderLegCollection") or []
            body = {
                "orderStrategyType": "SINGLE", "orderType": cur.get("orderType", "STOP"),
                "session": cur.get("session", "NORMAL"), "duration": cur.get("duration", "DAY"),
                "stopPrice": fmt(price),
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
                                  f"{fmt(Decimal(str(cur.get('stopPrice') or 0)))} -> {fmt(price)}")
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
        notes = [f"Stop moved to {fmt(price)} on {covered:g} of {abs(qty):g} shares."]
        if had_oco:
            if live_targets and oco_kept:
                notes.append(f"OCO kept: target(s) still working ({tq:g} sh) and linked to the stop.")
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
        return {"ok": ok, "symbol": symbol, "stop": fmt(price), "moved": moved, "of": len(stops),
                "oco_kept": oco_kept, "message": " ".join(notes), "error": None if ok else " ".join(notes)}

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
        if asset != "EQUITY":
            return {"ok": False, "symbol": symbol, "error": "Trailing stops here are for stocks only."}
        side = "LONG" if qty > 0 else "SHORT"
        rps = None
        if (unit or "").upper() == "R":
            if not rbasis or rbasis.get("side") != side or not rbasis.get("rps"):
                return {"ok": False, "symbol": symbol,
                        "error": "A trail in R needs the frozen 1R of a companion entry. Use $ or %."}
            rps = Decimal(str(rbasis["rps"]))
        spec, err = trail_spec(unit, value, rps, basis)
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
        q = self.quote(symbol, max_age=0)
        ref = Decimal(str(_basis_price(q, spec["basis"]) or 0))
        start = trail_level(ref, spec, side) if ref else None
        looser = (start is not None and stop is not None
                  and (start < Decimal(str(stop)) if side == "LONG" else start > Decimal(str(stop))))
        note = (f" It starts about {fmt(start)}, looser than your stop {fmt(Decimal(str(stop)))} by "
                f"{fmt(abs(start - Decimal(str(stop))))}/sh." if looser else "")
        self.store.log_action("trail", symbol, None, "SENDING",
                              f"{_trail_txt(spec)}, {abs(qty):g} sh" + (f", start ~{fmt(start)}" if start else ""))
        res = self._create_stop(client, symbol, side, abs(qty), asset, None, [], set(),
                                target=Decimal(str(target)) if target is not None else None, rebuild=True, trail=spec)
        msg = (res.get("message") or res.get("error") or "Not confirmed: check thinkorswim.") + note
        self.store.log_action("trail", symbol, None, "DONE" if res.get("ok") else "CHECK", msg)
        return {"ok": bool(res.get("ok")), "symbol": symbol, "message": msg, "looser": looser,
                "start": fmt(start) if start else None, "error": None if res.get("ok") else msg}

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
        start = trail_level(Decimal(str(ref)), trail, side)
        prior = Decimal(str(prior_level))
        looser = start < prior if side == "LONG" else start > prior
        return (f" The trail restarts about {fmt(start)}, looser than the ~{fmt(prior)} it had reached."
                if looser else f" The trail restarts about {fmt(start)}.")

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
        if r_mult:
            b = basis
            if not b or b.get("side") != side or not b.get("rps"):
                return {"ok": False, "symbol": symbol,
                        "error": "R needs the entry and original stop of a companion ticket. Type a price instead."}
            move = Decimal(str(b["rps"])) * Decimal(str(r_mult))
            raw = Decimal(str(b["entry"])) + (move if side == "LONG" else -move)
            target = _to_tick(raw, ROUND_FLOOR if side == "LONG" else ROUND_CEILING)
        else:
            try:
                target = _to_tick(Decimal(str(price)), ROUND_HALF_UP)
            except Exception:
                return {"ok": False, "symbol": symbol, "error": "Enter a target price."}
        q = self.quote(symbol, max_age=0)
        bid, ask = Decimal(str(q.get("bid") or 0)), Decimal(str(q.get("ask") or 0))
        if target <= 0 or (side == "LONG" and ask and target <= ask) or (side == "SHORT" and bid and target >= bid):
            return {"ok": False, "symbol": symbol,
                    "error": f"Target {fmt(target)} is at or through the market ({fmt(bid)} / {fmt(ask)}): "
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
        label = f"{r_mult:g}R = {fmt(target)}" if r_mult else fmt(target)
        if trail:
            self.store.log_action("set_target", symbol, None, "SENDING", f"target {label} + {_trail_txt(trail)}")
            res = self._create_stop(client, symbol, side, abs(qty), asset, None, [], set(), target=target,
                                    rebuild=True, trail=trail)
            msg = ((f"Target set at {label} with the trailing stop ({_trail_txt(trail)}) as one OCO."
                    if res.get("ok") else res.get("error") or res.get("message") or "Not confirmed: check thinkorswim.")
                   + self._restart_note(symbol, side, trail, prior_trail_level))
            self.store.log_action("set_target", symbol, None, "DONE" if res.get("ok") else "CHECK", msg)
            return {"ok": bool(res.get("ok")), "symbol": symbol, "target": fmt(target), "message": msg,
                    "error": None if res.get("ok") else msg}
        if stop is None:
            # No stop to pair it with: a lone target (the row keeps showing NO STOP SEEN).
            instr = {("EQUITY", True): "SELL", ("EQUITY", False): "BUY_TO_COVER",
                     ("OPTION", True): "SELL_TO_CLOSE", ("OPTION", False): "BUY_TO_CLOSE"}.get((asset, side == "LONG"))
            self.store.log_action("set_target", symbol, None, "SENDING", f"target {label}, no stop")
            r = self._place_exit(client, symbol, instr, asset, int(abs(qty)), target)
            ok = r.status_code in (200, 201)
            msg = (f"Target set at {label} for {abs(qty):g} shares. WARNING: there is no stop: use Stop to B/E "
                   "or Move stop." if ok else f"Schwab refused the target (HTTP {r.status_code}): {r.text[:150]}")
            self.store.log_action("set_target", symbol, None, "DONE" if ok else "REJECTED", msg)
            return {"ok": ok, "symbol": symbol, "message": msg, "error": None if ok else msg}
        self.store.log_action("set_target", symbol, None, "SENDING", f"target {label} + stop {stop}, {abs(qty):g} sh")
        res = self._create_stop(client, symbol, side, abs(qty), asset, Decimal(str(stop)), [], set(),
                                target=target, rebuild=True)
        msg = (f"Target set at {label} with stop {stop} for {abs(qty):g} shares as one OCO."
               if res.get("ok") else res.get("error") or res.get("message") or "Not confirmed: check thinkorswim.")
        self.store.log_action("set_target", symbol, None, "DONE" if res.get("ok") else "CHECK", msg)
        return {"ok": bool(res.get("ok")), "symbol": symbol, "target": fmt(target), "message": msg,
                "error": None if res.get("ok") else msg}

    # ---- partials --------------------------------------------------------------------------

    def take_partial(self, symbol: str, pct: float | None, shares: float | None,
                     orders: list[dict], known_terminal: set[int], mode: str = "mid",
                     prior_trail_level=None) -> dict:
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
                                        mode if mode in ("mid", "market", "ask", "bid") else "mid", prior_trail_level)
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
            order["price"] = fmt(price)
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
        if mode in ("mid", "ask", "bid"):
            q = self.quote(symbol, max_age=0)
            bid, ask = Decimal(str(q.get("bid") or 0)), Decimal(str(q.get("ask") or 0))
            if mode == "mid" and bid > 0 and ask > bid + tick(bid):
                price = _to_tick((bid + ask) / 2, ROUND_FLOOR if selling else "ROUND_CEILING")
            elif mode == "bid" and bid > 0:
                price = _to_tick(bid, ROUND_FLOOR)          # literal bid (takes for a sell, rests for a cover)
            elif mode == "ask" and ask > 0:
                price = _to_tick(ask, "ROUND_CEILING")      # literal ask (rests for a sell, takes for a cover)
        how = f"limit {fmt(price)} at the {where}" if price else "market"
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
                        mode: str = "mid", prior_trail_level=None) -> dict:
        from .orders import pending_orders
        client = self._ensure()
        blocked = self._blocked(symbol, client)
        if blocked:
            return blocked
        qty, asset = self._position(client, symbol)
        if abs(qty) < 1e-9:
            return {"ok": False, "symbol": symbol, "error": f"No open {symbol} position."}
        side = "LONG" if qty > 0 else "SHORT"
        size = abs(qty)
        if shares:
            n = int(float(shares))
        else:
            n = int(size * float(pct or 0))
        if n < 1:
            return {"ok": False, "symbol": symbol, "error": f"{pct or shares} of {size:g} shares is less than 1 share."}
        if n >= size:
            return self._close_locked(symbol, orders, known_terminal)
        instr = {("EQUITY", True): "SELL", ("EQUITY", False): "BUY_TO_COVER",
                 ("OPTION", True): "SELL_TO_CLOSE", ("OPTION", False): "BUY_TO_CLOSE"}.get((asset, side == "LONG"))
        if not instr:
            return {"ok": False, "symbol": symbol, "error": f"Cannot take a partial on a {asset} position here."}
        fresh = self._fresh_orders(client, orders)
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
            size_note = (f" CHECK: Schwab shows {abs(left):g} shares, expected {expected:g}; protecting "
                         f"{min(abs(left), expected):g}.")
        if sent.get("unresolved"):
            # The partial may still fill: protect only what is left even if all of it fills, so the
            # new exits can never be larger than the position.
            expected = size - n
            size_note += (f" The partial is not confirmed final: protecting {min(abs(left), expected):g} shares (what "
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
                msg += f" CHECK: the {abs(left):g} remaining shares may have no stop. Use Move stop or thinkorswim."
            return {"ok": bool(res.get("ok")) and filled >= n - 1e-9, "symbol": symbol, "filled": filled, "avg": avg,
                    "message": msg, "error": None if res.get("ok") and filled >= n - 1e-9 else msg}
        if stop is None and target is None:
            return {"ok": filled > 0, "symbol": symbol,
                    "message": head + f" {abs(left):g} shares left; there was no stop or target to put back."}
        if stop is None:
            return {"ok": False, "symbol": symbol,
                    "error": head + f" {abs(left):g} shares left with NO STOP (there was none before). "
                                    "Use Stop to B/E or Move stop."}
        res = self._create_stop(client, symbol, side, left, asset, Decimal(str(stop)), [], set(),
                                target=Decimal(str(target)) if target is not None else None, rebuild=True)
        msg = head + " " + (res.get("message") or res.get("error") or "")
        if not res.get("ok"):
            msg += f" CHECK: the {abs(left):g} remaining shares may have no stop. Use Move stop or thinkorswim."
        return {"ok": bool(res.get("ok")) and filled >= n - 1e-9, "symbol": symbol, "filled": filled, "avg": avg,
                "message": msg, "error": None if res.get("ok") and filled >= n - 1e-9 else msg,
                "unresolved_close": bool(sent.get("unresolved"))}

    def _create_stop(self, client, symbol: str, side: str, qty: float, asset: str | None,
                     price: Decimal | None, fresh: list[dict], terminal: set[int],
                     target: Decimal | None = None, rebuild: bool = False, trail: dict | None = None) -> dict:
        """Create a stop for the whole position. Schwab cancels an instrument's working orders
        when a new order is entered, so an existing single target is re-sent together with the
        new stop as one OCO (the target survives and gets linked).

        rebuild=True (after a partial): Schwab already cancelled the old exits; send exactly
        `target` (if any) + stop for `qty`, without looking at what is working."""
        from .orders import pending_orders
        instr = {("EQUITY", True): "SELL", ("EQUITY", False): "BUY_TO_COVER",
                 ("OPTION", True): "SELL_TO_CLOSE", ("OPTION", False): "BUY_TO_CLOSE"}.get((asset, side == "LONG"))
        if not instr:
            return {"ok": False, "symbol": symbol, "error": f"Cannot set a stop on a {asset} position here."}
        size = abs(qty)
        n = int(size) if float(size).is_integer() else size
        leg = [{"instruction": instr, "quantity": n, "instrument": {"symbol": symbol, "assetType": asset}}]
        from .bracket import stop_leg
        stop = stop_leg(trail or price, leg)
        stop_txt = _trail_txt(trail) if trail else f"stop {fmt(price)}"
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
                 "duration": "DAY", "price": fmt(target), "orderLegCollection": leg},
                stop]}
            what = f"{stop_txt} + target {fmt(target)} as one OCO"
            targets = targets or [True]
        else:
            order, what = stop, stop_txt
        self.store.log_action("create_stop", symbol, None, "SENDING", f"{what}, {n} sh")
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
        msg = ((f"Trailing stop ({_trail_txt(trail)}) working" if trail else f"Stop created at {fmt(price)}")
               + f" for {covered:g} of {size:g} shares"
               + (", linked with the target in one OCO." if targets and tgt_ok else ".")
               + ("" if tgt_ok else " WARNING: the target is not working now: check thinkorswim.")
               + ("" if covered >= size - 1e-9 else " Not confirmed yet: check thinkorswim."))
        self.store.log_action("create_stop", symbol, None, "DONE" if ok else "CHECK", msg)
        return {"ok": ok, "symbol": symbol, "stop": fmt(price) if price is not None else None, "created": True,
                "trail": bool(trail), "message": msg,
                "error": None if ok else msg}

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

        legs = o.get("orderLegCollection") or []
        if o.get("orderStrategyType") != "TRIGGER" or len(legs) != 1:
            return None
        entry = (legs[0].get("instruction"), (legs[0].get("instrument") or {}).get("symbol"),
                 num(legs[0].get("quantity") or o.get("quantity")), num(o.get("price")))
        exits = []
        for oco in o.get("childOrderStrategies") or []:
            kids = oco.get("childOrderStrategies") or []
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
        return (entry, tuple(sorted(exits))) if exits else None

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
        for t in self.store.tickets_since(start.isoformat()):   # newest first
            row = dict(t)
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
                row.update({"entry_status": tree.get("status"), "filled": filled})
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
        fill and the ORIGINAL stop. Later stop moves or adds never redefine R."""
        by_id = {o["orderId"]: o for o in orders_today}
        start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        out: dict[str, dict] = {}
        for t in self.store.tickets_since(start.isoformat()):   # newest first
            if t["symbol"] in out or t["status"] != "ACCEPTED" or not t["order_id"]:
                continue
            tree = by_id.get(t["order_id"])
            legs = [e for a in (tree or {}).get("orderActivityCollection") or []
                    if a.get("executionType") == "FILL" for e in a.get("executionLegs") or []]
            qty = sum(float(e.get("quantity") or 0) for e in legs)
            if qty <= 0:
                continue
            entry = sum(float(e["price"]) * float(e["quantity"]) for e in legs) / qty
            stop = float(t["stop"])
            rps = abs(entry - stop)
            first_fill = min((e.get("time") for e in legs if e.get("time")), default=None)
            if rps > 0:
                out[t["symbol"]] = {"side": t["side"], "entry": entry, "stop": stop, "rps": rps,
                                    "ticket_id": t["ticket_id"], "first_fill": first_fill}
        return out

    def status(self) -> dict:
        return {"armed": self.armed, "market_open": market_open(),
                "blocked": bool(self.store.open_tickets()), "risk_choices": RISK_CHOICES}
