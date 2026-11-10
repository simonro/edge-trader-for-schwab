"""Spread ticket and actions: slices 2, 3 and 5 of the spreads plan.

One exclusive vertical, sized by quantity or by Risk $ (spreads = risk / max loss of one spread):
a DAY net-limit entry, Cancel (of the unfilled rest too), one paired net-limit Close of what is
actually held, and a resting GTC profit target (a paired close at a % of the max gain, set after
the fill). Close first cancels a working target. No stop (Schwab's API refuses any stop on a
spread: the most it can lose is the max loss shown on the ticket). Every order goes out only on the user's click, and only after Schwab's
previewOrder accepted that exact order. Nothing here retries a POST.

The near leg is the strike closer to the money: bought for a debit spread, sold for a credit
spread. The far leg is further out (a higher strike for calls, a lower one for puts), so the
spread's value is always near minus far, between 0 and the strike width.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation

from ..bracket import PENNY_ALL_ROOTS, put_call
from ..orders import TERMINAL
from ..pnl import ET
from ..trading import INDEX_ROOTS, _odd_deliverable, _occ_root, _all_symbols, _nodes, market_open, regular_hours_text, terminal_ids
from .spreads import expiration_of, leg_fills

log = logging.getLogger("companion.spreads")

MAX_SPREADS = 20            # per ticket
MULT = 100
PENNY, NICKEL = Decimal("0.01"), Decimal("0.05")
WIDE_LEG = Decimal("0.15")   # a leg whose bid / ask is wider than 15% of its mid gets a warning
KINDS = ("DEBIT", "CREDIT")
QUOTE_AGE = 10.0             # seconds a display-only plan may reuse a leg quote


def _d(v) -> Decimal | None:
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return d if d.is_finite() else None


def is_index(root: str) -> bool:
    root = (root or "").upper().lstrip("$")
    return root in INDEX_ROOTS or (len(root) > 1 and root[:-1] in INDEX_ROOTS and root[-1] in "WP")


def spread_step(underlying: str, near_q: dict, far_q: dict) -> Decimal:
    """The net price grid of a vertical. Index spreads (SPX, SPXW...) trade in nickels (Schwab:
    "must be priced in 5-cent increments"); SPY / QQQ / IWM and spreads whose two legs are both
    penny classes in pennies; anything else in nickels (valid everywhere)."""
    und = (underlying or "").upper().lstrip("$")
    if is_index(und):
        return NICKEL
    if und in PENNY_ALL_ROOTS or (near_q.get("is_penny") is True and far_q.get("is_penny") is True):
        return PENNY
    return NICKEL


def on_step(v: Decimal, step: Decimal, up: bool) -> Decimal:
    return (v / step).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * step


def same_underlying(symbol: str, underlying: str) -> bool:
    root, und = _occ_root(symbol).upper(), (underlying or "").upper().lstrip("$")
    return root == und or (und in INDEX_ROOTS and root in (und + "W", und + "P"))


def _f(v: Decimal | None) -> str | None:
    return None if v is None else f"{v:.2f}"


def nets(near: dict, far: dict) -> dict:
    """The spread's net quotes from its legs: mid, what buying it costs at the market (near ask
    minus far bid) and what selling it brings (near bid minus far ask)."""
    nb, na, fb, fa = (_d(near.get("bid")), _d(near.get("ask")), _d(far.get("bid")), _d(far.get("ask")))
    if None in (nb, na, fb, fa):
        return {}
    return {"mid": (nb + na) / 2 - (fb + fa) / 2, "buy": na - fb, "sell": nb - fa}


def entry_order(legs: list[dict], buy: bool, price: Decimal, qty: int) -> dict:
    return {"orderStrategyType": "SINGLE", "orderType": "NET_DEBIT" if buy else "NET_CREDIT",
            "complexOrderStrategyType": "VERTICAL", "session": "NORMAL", "duration": "DAY",
            "price": f"{price:.2f}", "quantity": qty,
            "orderLegCollection": [{"instruction": "BUY_TO_OPEN" if l["side"] == "LONG" else "SELL_TO_OPEN",
                                    "quantity": qty, "instrument": {"symbol": l["symbol"], "assetType": "OPTION"}}
                                   for l in legs]}


def close_order(legs: list[dict], debit_spread: bool, price: Decimal, qty: int, duration: str = "DAY") -> dict:
    """The paired close: a debit spread is sold back (NET_CREDIT), a credit spread bought back
    (NET_DEBIT), both legs in one order. The profit target is the same order, GTC."""
    return {"orderStrategyType": "SINGLE", "orderType": "NET_CREDIT" if debit_spread else "NET_DEBIT",
            "complexOrderStrategyType": "VERTICAL", "session": "NORMAL", "duration": duration,
            "price": f"{price:.2f}", "quantity": qty,
            "orderLegCollection": [{"instruction": "SELL_TO_CLOSE" if l["side"] == "LONG" else "BUY_TO_CLOSE",
                                    "quantity": qty * abs(int(l.get("ratio") or 1)),
                                    "instrument": {"symbol": l["symbol"], "assetType": "OPTION"}}
                                   for l in legs]}


def fill_net(order: dict | None, sp: dict) -> Decimal | None:
    """The spread's net entry price per spread from its entry order's executions (a limit can fill
    better): credit = sold leg minus bought leg, debit the other way. None until both legs show fills."""
    if not order:
        return None
    by_leg = {leg.get("legId"): (leg.get("instrument") or {}).get("symbol") for leg in order.get("orderLegCollection") or []}
    cost: dict[str, Decimal] = {}
    qty: dict[str, Decimal] = {}
    for act in order.get("orderActivityCollection") or []:
        if act.get("executionType") != "FILL":
            continue
        for ex in act.get("executionLegs") or []:
            sym, q, px = by_leg.get(ex.get("legId")), _d(ex.get("quantity")), _d(ex.get("price"))
            if sym and q and px is not None:
                cost[sym] = cost.get(sym, Decimal(0)) + q * px
                qty[sym] = qty.get(sym, Decimal(0)) + q
    filled = _d(order.get("filledQuantity")) or Decimal(0)
    if filled <= 0 or any(qty.get(l["symbol"], Decimal(0)) != filled * abs(int(l.get("ratio") or 1)) for l in sp["legs"]):
        return None          # prices for only part of the fill: not the entry yet
    sold = sum(cost[l["symbol"]] / qty[l["symbol"]] for l in sp["legs"] if l["side"] == "SHORT")
    bought = sum(cost[l["symbol"]] / qty[l["symbol"]] for l in sp["legs"] if l["side"] == "LONG")
    return sold - bought if sp["kind"].endswith("CREDIT") else bought - sold


def _not_settled(sp: dict, role: str, order: dict) -> str | None:
    """Why a final order of the spread cannot be counted yet (None: it can). REPLACED means changed
    in thinkorswim: its replacement may still execute, so it is never proof that the order is gone.
    A filledQuantity its execution legs do not cover yet would count too few fills, so too many
    spreads would look held."""
    if order.get("status") == "REPLACED":
        return (f"The {role} was replaced outside the companion (changed in thinkorswim?): its replacement may "
                "still execute. Check it, then Resolve. Nothing was sent.")
    got = leg_fills(order)
    units = min((got.get(l["symbol"], 0.0) / abs(l["ratio"] or 1) for l in sp["legs"]), default=0.0)
    if float(order.get("filledQuantity") or 0) > units + 1e-9:
        return (f"Schwab shows the {role} with {float(order.get('filledQuantity') or 0):g} filled but has not listed "
                "all its executions yet. Nothing was sent: try again in a few seconds.")
    return None


TARGET_PCTS = (Decimal("0.25"), Decimal("0.50"), Decimal("0.75"))


def plain_refusal(text: str) -> str:
    """Schwab's spread refusals in plain words (docs/SCHWAB_API_NOTES.md, Vertical spreads)."""
    t = text or ""
    if "5-cent" in t:
        return "Index spreads are priced in 5-cent steps."
    if "cannot equal or exceed the strike difference" in t:
        return "The credit must be less than the strike width."
    if "incorrect credit or debit" in t:
        return "Debit / credit does not match the strikes picked."
    return t


def make_plan(quote, req: dict, fresh: bool = False) -> dict:
    """The vertical the ticket shows, from Schwab's leg quotes (never from the page): contracts,
    sides, net mid and natural, the entry limit, width, max loss and gain, breakeven."""
    errors, warnings = [], []
    und = str(req.get("underlying") or "").upper().strip().lstrip("$")
    kind = str(req.get("kind") or "").upper()
    near_s, far_s = str(req.get("near") or "").upper(), str(req.get("far") or "").upper()
    entry = str(req.get("entry") or "mid").lower()
    size_mode = str(req.get("size_mode") or "qty").lower()
    out = {"ok": False, "errors": errors, "warnings": warnings, "underlying": und, "kind": kind,
           "entry": entry, "size_mode": size_mode, "qty": None}
    if size_mode not in ("qty", "risk"):
        errors.append("size by Quantity or Risk $")
    if kind not in KINDS:
        errors.append("pick Debit or Credit")
    if not und or not near_s or not far_s:
        errors.append("pick the underlying and both strikes")
    if entry not in ("mid", "natural", "limit"):
        errors.append("pick Mid, Natural or Lmt")
    if near_s and near_s == far_s:
        errors.append("the two strikes must differ")
    if errors:
        return out
    # Request budget: the ticket refreshes every 5 s, so a plan reuses quotes up to 10 s old (about 12
    # requests a minute for both legs); the click itself always reads fresh.
    qn, qf = quote(near_s, max_age=0 if fresh else QUOTE_AGE), quote(far_s, max_age=0 if fresh else QUOTE_AGE)
    for name, q, s in (("near", qn, near_s), ("far", qf, far_s)):
        if not q.get("found") or q.get("asset_type") != "OPTION":
            errors.append(f"{s.strip()}: not an option Schwab knows")
            continue
        if float(q.get("multiplier") or MULT) != MULT:
            errors.append(f"{s.strip()}: multiplier {q.get('multiplier')}, only standard 100-share contracts")
        odd = _odd_deliverable(q, s)
        if odd:
            errors.append(f"{s.strip()}: non-standard contract ({odd})")
        if not same_underlying(s, und):
            errors.append(f"{s.strip()} is not an option on {und}")
    if errors:
        return out
    # The type comes from the contract symbol itself (Trader.quote reports it as "C" / "P").
    types = {put_call(near_s), put_call(far_s)}
    pc = {"C": "CALL", "P": "PUT"}.get(put_call(near_s))
    if len(types) != 1 or pc is None:
        errors.append("both legs must be calls, or both puts")
    exp_n, exp_f = expiration_of(near_s), expiration_of(far_s)
    if exp_n is None or exp_n != exp_f:
        errors.append("both legs must have the same expiry")
    kn, kf = _d(qn.get("strike")), _d(qf.get("strike"))
    if kn is None or kf is None:
        errors.append("strike not in Schwab's quote")
    if errors:
        return out
    if (pc == "CALL" and kf <= kn) or (pc == "PUT" and kf >= kn):
        errors.append("the second strike must be further out of the money "
                      f"({'higher' if pc == 'CALL' else 'lower'} than {kn:g} for {pc.lower()}s)")
        return out
    buy = kind == "DEBIT"
    width = abs(kf - kn)
    step = spread_step(und, qn, qf)
    n = nets(qn, qf)
    legs = [{"symbol": near_s, "side": "LONG" if buy else "SHORT", "strike": float(kn),
             "bid": qn.get("bid"), "ask": qn.get("ask"), "ratio": 1},
            {"symbol": far_s, "side": "SHORT" if buy else "LONG", "strike": float(kf),
             "bid": qf.get("bid"), "ask": qf.get("ask"), "ratio": 1}]
    out.update({"put_call": pc, "expiration": exp_n.isoformat(), "width": _f(width), "step": _f(step),
                "legs": legs, "near_strike": float(kn), "far_strike": float(kf)})
    if not n:
        errors.append("one of the legs has no bid / ask right now")
        return out
    for q, s in ((qn, near_s), (qf, far_s)):
        if _d(q.get("bid")) > _d(q.get("ask")) or _d(q.get("bid")) < 0:
            errors.append(f"{s.strip()} has a crossed or invalid market ({q.get('bid')} x {q.get('ask')})")
        if q.get("realtime") is False:
            errors.append(f"{s.strip()}: Schwab's quote is delayed, not real time")
    if errors:
        return out
    natural = n["buy"] if buy else n["sell"]
    out.update({"mid": _f(n["mid"]), "natural": _f(natural)})
    if _d(qn.get("ask")) <= 0 or n["mid"] <= 0:
        errors.append("this spread has no value to trade at the moment")
        return out
    for q, s in ((qn, near_s), (qf, far_s)):
        b, a = _d(q.get("bid")), _d(q.get("ask"))
        # Wide in both senses: over 15% of the mid AND more than a nickel (a 0.11 x 0.13 wing is fine).
        if a and b is not None and a > 0 and a - b > NICKEL and (a - b) / ((a + b) / 2 or a) > WIDE_LEG:
            warnings.append(f"{s.strip()} has a wide market ({b:.2f} x {a:.2f})")
    if entry == "limit":
        price = _d(req.get("limit_price"))
        if price is None or price <= 0:
            errors.append("type a limit price")
            return out
        if price % step:
            errors.append(f"the limit must be in {step:.2f} steps")
            return out
    else:
        price = on_step(n["mid"] if entry == "mid" else natural, step, up=buy)
    if price < step:
        price = step
    if price >= width:
        errors.append(f"the {'debit' if buy else 'credit'} must be less than the strike width {width:.2f}")
        return out
    if natural <= 0 and not buy:
        warnings.append("no credit at the market right now (bid side): a credit limit may not fill")
    if (buy and price > natural) or (not buy and price < natural):
        warnings.append(f"the limit is through the market ({natural:.2f}): it fills at once")
    loss1 = (price if buy else width - price) * MULT          # one spread
    gain1 = (width - price if buy else price) * MULT
    out.update({"limit": _f(price), "max_loss_each": _f(loss1), "max_gain_each": _f(gain1)})
    if size_mode == "risk":
        risk = _d(req.get("risk"))
        if risk is None or risk <= 0:
            errors.append("pick a Risk $ amount")
            return out
        qty = int(risk // loss1)
        if qty < 1:
            errors.append(f"Risk ${risk:,.2f} is less than one spread's max loss (${loss1:,.2f})")
            return out
    else:
        try:
            qty = int(str(req.get("qty") or "").strip())
        except ValueError:
            qty = 0
        if qty < 1:
            errors.append("type how many spreads")
            return out
    if qty > MAX_SPREADS:
        errors.append(f"at most {MAX_SPREADS} spreads per ticket")
        return out
    be = kn + price if pc == "CALL" else kn - price
    out.update({"ok": True, "qty": qty, "max_loss": _f(loss1 * qty), "max_gain": _f(gain1 * qty),
                "breakeven": _f(be), "dte": (exp_n - datetime.now(ET).date()).days})
    if out["dte"] < 0:
        errors.append("this expiry has passed")
        out["ok"] = False
    return out


class SpreadDesk:
    """The spread ticket's order actions, on the Trader's client, lock and request log."""

    def __init__(self, trader):
        self.t = trader

    @property
    def book(self):
        return self.t.spreads

    def plan(self, req: dict, fresh: bool = False) -> dict:
        return make_plan(self.t.quote, req, fresh)

    def _preview(self, client, order: dict) -> dict:
        refused, body = self.t._preview_answer(client, order)
        if refused:
            rejected, why = refused
            return {"ok": False, "rejected": rejected,
                    "error": (plain_refusal(why) if rejected else f"Schwab's preview gave no clear answer ({why})")}
        bal = ((body or {}).get("orderStrategy") or {}).get("orderBalance") or {}
        return {"ok": True, "balance": {k: bal.get(k) for k in ("orderValue", "projectedBuyingPower",
                                                                 "projectedAvailableFund", "projectedCommission")}}

    def preview(self, req: dict) -> dict:
        """Schwab's preview of the ticket's entry (places nothing): accepted or why not, and the
        buying-power effect."""
        p = self.plan(req, fresh=True)
        if not p["ok"]:
            return {"ok": False, "plan": p, "error": "; ".join(p["errors"])}
        order = entry_order(p["legs"], p["kind"] == "DEBIT", Decimal(p["limit"]), p["qty"])
        return {**self._preview(self.t._ensure(), order), "plan": p}

    # ---- entry ------------------------------------------------------------------------------

    def submit(self, req: dict) -> dict:
        """Send exactly the spread the ticket displayed (contracts, debit/credit, limit), or nothing."""
        if not self.book:
            return {"ok": False, "error": "Spreads are not available in this build."}
        ticket_id = str(req.get("ticket_id") or "")
        if len(ticket_id) < 8:
            return {"ok": False, "error": "missing ticket id"}
        prior = self.book.by_ticket(ticket_id)
        if prior:      # the same intent clicked again: report, never resend
            return {"ok": prior["entry_order_id"] is not None, "duplicate": True, "spread_id": prior["spread_id"],
                    "order_id": prior["entry_order_id"], "state": prior["state"],
                    "error": None if prior["entry_order_id"] else f"This ticket was already sent: {prior['state']}. "
                                                                   "Check thinkorswim."}
        if not self.t.armed:
            return {"ok": False, "error": "Trading is not armed. Use the ARM switch on the ticket."}
        if not market_open():
            return {"ok": False, "error": regular_hours_text()}
        if self.t.store.open_tickets():
            return {"ok": False, "error": "A previous ticket is not confirmed yet. Check thinkorswim, then press Resolve."}
        stuck = self.book.unconfirmed()
        if stuck:
            return {"ok": False, "spread_id": stuck["spread_id"],
                    "error": "A previous spread ticket has no confirmed order. Check thinkorswim, then Resolve it."}
        if not self.t._send_lock.acquire(blocking=False):
            return {"ok": False, "error": "Another order action is in progress."}
        state = {"sid": None}
        try:
            return self._submit_locked(req, ticket_id, state)
        except Exception as e:
            log.exception("spread submit failed")
            if state["sid"] is None:
                return {"ok": False, "error": f"Not sent: {e}"[:300]}
            self.book.set_state(state["sid"], "UNKNOWN", f"error after sending: {e!r}"[:300])
            return {"ok": False, "spread_id": state["sid"], "status": "UNKNOWN",
                    "error": "Error after sending. The spread MAY be live: check thinkorswim."}
        finally:
            self.t._send_lock.release()

    def _submit_locked(self, req: dict, ticket_id: str, state: dict) -> dict:
        exp_limit, exp_loss = _d(req.get("expected_limit")), _d(req.get("expected_max_loss"))
        try:
            exp_qty = int(req.get("expected_qty") or 0)
        except (TypeError, ValueError):
            exp_qty = 0
        if exp_limit is None or exp_loss is None or exp_limit <= 0 or exp_qty < 1:
            return {"ok": False, "error": "ticket is missing the displayed plan; review and click again"}
        # Exactly what was displayed: its limit and its number of spreads (never re-sized at send), from
        # fresh leg quotes.
        p = self.plan({**req, "entry": "limit", "limit_price": str(exp_limit), "size_mode": "qty",
                       "qty": exp_qty}, fresh=True)
        if not p["ok"]:
            return {"ok": False, "error": "; ".join(p["errors"]) or "no valid quote", "plan": p}
        if Decimal(p["max_loss"]) != exp_loss:
            return {"ok": False, "error": f"Max loss is now ${p['max_loss']} (ticket showed ${exp_loss:.2f}). "
                                          "Review and click again.", "plan": p}
        client = self.t._ensure()
        legs = p["legs"]
        syms = [l["symbol"] for l in legs]
        gate = self.t._spread_gate(syms)
        if gate:
            return {"ok": False, "error": gate["error"]}
        for s in syms:
            blocked = self.t._blocked(s, client)
            if blocked:
                return {"ok": False, "error": blocked["error"]}
        # One exclusive vertical: the account holds neither contract and no working order touches them.
        held = {(x.get("instrument") or {}).get("symbol"): float(x.get("longQuantity") or 0) - float(x.get("shortQuantity") or 0)
                for x in self.t._positions(client)}
        mixed = [s.strip() for s in syms if abs(held.get(s, 0.0)) > 1e-9]
        if mixed:
            return {"ok": False, "error": f"The account already holds {', '.join(mixed)}: a spread ticket only "
                                          "opens contracts you do not hold. Nothing was sent."}
        working = self._working_on(client, set(syms))
        if working:
            return {"ok": False, "error": f"Working order(s) {', '.join(map(str, working))} already touch these "
                                          "contracts. Nothing was sent."}
        order = entry_order(legs, p["kind"] == "DEBIT", exp_limit, exp_qty)
        pv = self._preview(client, order)
        if not pv["ok"]:
            return {"ok": False, "error": f"Schwab's preview refused it: {pv['error']}. Nothing was sent."
                    if pv.get("rejected") else f"{pv['error']}. Nothing was sent.", "plan": p}
        ticket = {"put_call": p["put_call"], "width": float(p["width"]), "entry_limit": float(exp_limit),
                  "max_loss": float(p["max_loss"]), "max_gain": float(p["max_gain"]),
                  "breakeven": float(p["breakeven"]), "request": order, "preview": pv["balance"]}
        sid = self.book.create(ticket_id, p["underlying"], f"{p['put_call']}_{p['kind']}",
                               [{"symbol": l["symbol"], "side": l["side"], "ratio": 1} for l in legs], exp_qty,
                               state="SENDING", ticket=ticket)
        if sid is None:
            return {"ok": False, "error": "A contract of this spread is already a leg of another companion spread."}
        state["sid"] = sid
        log.warning("PLACE spread %s x%s %s %s @ %s", p["kind"], exp_qty, syms[0].strip(), syms[1].strip(), exp_limit)
        self.t.store.log_action("spread_entry", p["underlying"], None, "SENDING", sid)
        try:
            r = self.t._logged("place_order", lambda: client.place_order(self.t._hash, order))
        except Exception as e:
            self.book.set_state(sid, "UNKNOWN", f"no answer from Schwab ({type(e).__name__}): check thinkorswim, "
                                                "then Resolve")
            return {"ok": False, "status": "UNKNOWN", "spread_id": sid,
                    "error": "No answer from Schwab. The spread MAY be live: check thinkorswim."}
        if r.status_code in (200, 201):
            tail = r.headers.get("Location", "").rstrip("/").split("/")[-1]
            oid = int(tail) if tail.isdigit() else None
            if oid is None:
                self.book.set_state(sid, "UNKNOWN", "Schwab accepted it without an order id: check thinkorswim, "
                                                    "then Resolve")
                return {"ok": False, "status": "UNKNOWN", "spread_id": sid,
                        "error": "Schwab accepted the spread but gave no order id. Check thinkorswim."}
            self.book.attach_order(sid, "entry", oid)
            self.book.set_state(sid, "PENDING", "entry sent")
            self.t.store.log_action("spread_entry", p["underlying"], oid, "ACCEPTED", sid)
            return {"ok": True, "spread_id": sid, "order_id": oid, "plan": p, "balance": pv["balance"]}
        if r.status_code == 429:
            self.book.set_state(sid, "CANCELED", "throttled (HTTP 429): not placed")
            return {"ok": False, "status": "RATE_LIMITED", "spread_id": sid, "retry_after": r.headers.get("Retry-After"),
                    "error": "Schwab throttled the order request (HTTP 429): nothing was placed. Click again to retry."}
        if 400 <= r.status_code < 500:
            self.book.set_state(sid, "CANCELED", f"rejected: HTTP {r.status_code} {r.text[:200]}")
            return {"ok": False, "status": "REJECTED", "spread_id": sid,
                    "error": f"Schwab rejected the spread (HTTP {r.status_code}): {plain_refusal(r.text[:300])}"}
        self.book.set_state(sid, "UNKNOWN", f"HTTP {r.status_code}: check thinkorswim, then Resolve")
        return {"ok": False, "status": "UNKNOWN", "spread_id": sid,
                "error": f"Schwab answered HTTP {r.status_code}. The spread MAY be live: check thinkorswim."}

    def _working_on(self, client, syms: set[str], exclude: frozenset = frozenset()) -> list[int]:
        """Order ids, read now, that can still execute and touch any of these contracts (today's
        orders plus the poller's 59-day working sweep). `exclude`: the spread's own orders this action
        just confirmed final at Schwab (an older GTC target stays WORKING in the sweep until its next run)."""
        if self.sweep_fresh is None or not self.sweep_fresh():
            raise RuntimeError("the older working orders (59-day sweep) are not current, so other orders on these "
                               "contracts cannot be ruled out; wait for the GTC dot to turn green")
        orders = self.t._fresh_orders(client, self._snapshot())
        terminal = terminal_ids(orders)
        return sorted({o.get("orderId") for o in _nodes(orders)
                       if o.get("orderId") not in terminal and o.get("orderId") not in exclude
                       and o.get("status") not in TERMINAL
                       and _all_symbols({"orderLegCollection": o.get("orderLegCollection")}) & syms})

    snapshot = None          # set by the routes: the poller's merged orders
    sweep_fresh = None       # set by the routes: whether the poller's 59-day working-order sweep is current

    def _snapshot(self) -> list[dict]:
        return self.snapshot() if self.snapshot else []

    # ---- cancel -----------------------------------------------------------------------------

    def cancel(self, spread_id: str) -> dict:
        """Cancel the spread's own working order (the entry, or a close) while nothing of it has
        filled. Only removes risk: no arming needed."""
        sp = self.book.get(spread_id) if self.book else None
        if not sp:
            return {"ok": False, "error": "No such spread."}
        if not self.t._send_lock.acquire(blocking=False):
            return {"ok": False, "error": "Another order action is in progress."}
        try:
            return self._cancel_locked(sp)
        except Exception as e:
            log.exception("spread cancel failed")
            return {"ok": False, "spread_id": spread_id, "error": f"Cancel failed: {e}"[:300]}
        finally:
            self.t._send_lock.release()

    def _cancel_locked(self, sp: dict, only: str | None = None) -> dict:
        """Cancel the spread's working close or entry, or (only="target") its target. "working": False
        in the answer means none of them was working (each found final, its fills recorded)."""
        sid = sp["spread_id"]
        client = self.t._ensure()
        roles = (("target", sp.get("target_order_id")),) if only == "target" else \
            (("close", sp["close_order_id"]), ("entry", sp["entry_order_id"]))
        for role, oid in roles:
            if oid is None or (only and role != only):
                continue
            r = self.t._logged("order_details", lambda o=oid: client.order_details(self.t._hash, o))
            if r.status_code != 200:
                return {"ok": False, "spread_id": sid, "error": f"Could not read order {oid} (HTTP {r.status_code})."}
            o = r.json()
            if o.get("status") in TERMINAL:
                bad = _not_settled(sp, role, o)
                if bad:
                    return {"ok": False, "spread_id": sid, "status": o.get("status"), "error": bad}
                self.book._save_fills(sid, role, o)
                continue
            if _all_symbols(o) != {l["symbol"] for l in sp["legs"]}:
                return {"ok": False, "spread_id": sid, "error": f"Order {oid} does not match this spread's contracts."}
            self.t.store.log_action("spread_cancel", sp["underlying"], oid, "SENDING", sid)
            try:
                r = self.t._logged("cancel_order", lambda o=oid: client.cancel_order(self.t._hash, o))
            except Exception as e:
                self.t.store.log_action("spread_cancel", sp["underlying"], oid, "UNKNOWN", repr(e)[:300])
                return {"ok": False, "spread_id": sid,
                        "error": "No answer from Schwab: the order may or may not be cancelled. Check thinkorswim."}
            if r.status_code not in (200, 201, 204):
                self.t.store.log_action("spread_cancel", sp["underlying"], oid, "REJECTED", f"HTTP {r.status_code}")
                return {"ok": False, "spread_id": sid,
                        "error": f"Schwab refused the cancel (HTTP {r.status_code}). It may have filled."}
            status, filled, final = "UNCONFIRMED", None, None
            for _ in range(10):
                time.sleep(0.25)
                a = self.t._logged("order_details", lambda o=oid: client.order_details(self.t._hash, o))
                if a.status_code != 200:
                    continue
                final = a.json()
                status, filled = final.get("status", "UNCONFIRMED"), float(final.get("filledQuantity") or 0)
                if status in TERMINAL:
                    break
            self.t.store.log_action("spread_cancel", sp["underlying"], oid, status, f"filled {filled}")
            if final is not None and status in TERMINAL:
                bad = _not_settled(sp, role, final)
                if bad:
                    return {"ok": False, "spread_id": sid, "status": status, "error": bad}
                # Record the order's final fills now: a part that filled before the cancel is held (or
                # closed) and every later step counts it, whatever the poller has seen yet.
                self.book._save_fills(sid, role, final)
            want = float(o.get("quantity") or 0)
            if status == "FILLED":
                return {"ok": False, "spread_id": sid, "status": status,
                        "error": f"The {role} filled completely before the cancel. Check the spread."}
            if status == "CANCELED":
                part = f" ({filled:g} of {want:g} had filled; the rest is cancelled)" if filled else ""
                return {"ok": True, "spread_id": sid, "order_id": oid, "status": status, "role": role,
                        "filled": filled, "want": want, "message": f"Cancelled the {role}{part}."}
            return {"ok": False, "spread_id": sid, "status": status,
                    "error": f"Cancel requested; Schwab still shows {status}. It can fill until Schwab confirms."}
        return {"ok": False, "spread_id": sid, "working": False, "error": "Nothing of this spread is working at Schwab."}

    # ---- close ------------------------------------------------------------------------------

    held_quotes = None       # set by the routes: the poller's quotes (it already polls every held contract)

    def _held_quote(self, symbol: str) -> dict:
        """A display-only leg quote at no request cost: the poller's own quote of a held contract when
        current, else the ticket quote cache (up to QUOTE_AGE old)."""
        q = self.held_quotes(symbol) if self.held_quotes else None
        return q if q else self.t.quote(symbol, max_age=QUOTE_AGE)

    def close_plan(self, spread_id: str, entry: str = "mid", limit_price=None, fresh: bool = False) -> dict:
        """The paired close the Close button would send: net mid and natural from fresh leg quotes,
        the limit, and the open P&L against the entry limit."""
        sp = self.book.get(spread_id) if self.book else None
        if not sp:
            return {"ok": False, "errors": ["No such spread."]}
        t = sp.get("ticket") or {}
        debit = sp["kind"].endswith("DEBIT")
        legs = sp["legs"]
        near = next((l for l in legs if (l["side"] == "LONG") == debit), legs[0])
        far = next(l for l in legs if l is not near)
        qn, qf = (self.t.quote(l["symbol"], max_age=0) if fresh else self._held_quote(l["symbol"]) for l in (near, far))
        width = _d(t.get("width"))
        out = {"ok": False, "errors": [], "spread_id": spread_id, "state": sp["state"], "entry": entry,
               "width": _f(width), "entry_limit": _f(_d(t.get("entry_limit")))}
        n = nets(qn, qf)
        if not n or width is None:
            out["errors"].append("no bid / ask on a leg right now" if not n else "the ticket record is missing")
            return out
        if any(_d(q.get("bid")) > _d(q.get("ask")) or q.get("realtime") is False for q in (qn, qf)):
            out["errors"].append("a leg's quote is crossed or delayed right now")
            return out
        step = spread_step(sp["underlying"], qn, qf)
        natural = n["sell"] if debit else n["buy"]
        if entry == "limit":
            price = _d(limit_price)
            if price is None or price <= 0 or price % step:
                out["errors"].append(f"type a limit in {step:.2f} steps")
                return out
        else:
            price = on_step(n["mid"] if entry == "mid" else natural, step, up=not debit)
        price = max(price, step)
        if price > width:
            out["errors"].append(f"the limit is above the strike width {width:.2f}")
            return out
        el = _d(t.get("entry_limit"))
        held = self.book.held_units(sp)
        pnl = None if el is None else ((n["mid"] - el) if debit else (el - n["mid"])) * MULT * Decimal(str(held))
        out["working"] = self.working_orders(sp)
        out.update({"ok": True, "limit": _f(price), "mid": _f(n["mid"]), "natural": _f(natural), "step": _f(step),
                    "open_pnl_mid": _f(pnl), "side": "sell" if debit else "buy", "held": held})
        return out

    def working_orders(self, sp: dict) -> list[dict]:
        """The spread's own orders last seen not final (from the store the poller keeps): what the
        workbench shows as working, with its limit."""
        out = []
        for role, oid in (("entry", sp["entry_order_id"]), ("close", sp["close_order_id"]),
                          ("target", sp.get("target_order_id"))):
            o = self.t.store.get_order(oid) if oid is not None else None
            if o and o.get("status") not in TERMINAL:
                out.append({"role": role, "order_id": oid, "status": o.get("status"), "price": o.get("price"),
                            "filled": o.get("filledQuantity"), "quantity": o.get("quantity"),
                            "type": o.get("orderType"), "duration": o.get("duration")})
        return out

    def close(self, req: dict) -> dict:
        """One paired net-limit close of an OPEN spread, after a fresh read shows the account holds
        exactly the spread and nothing else is working on its contracts. Never resent: an unclear
        outcome holds the spread (NEEDS_ACTION) until Resolve."""
        sid = str(req.get("spread_id") or "")
        sp = self.book.get(sid) if self.book else None
        if not sp:
            return {"ok": False, "error": "No such spread."}
        if not market_open():
            return {"ok": False, "spread_id": sid, "error": regular_hours_text()}
        if not self.t._send_lock.acquire(blocking=False):
            return {"ok": False, "spread_id": sid, "error": "Another order action is in progress."}
        sent = {"v": False}
        try:
            r = self._close_locked(self.book.get(sid), req, sent)
            if not r["ok"] and sent.get("target_canceled") and not sent["v"]:
                r["error"] += " The target was cancelled first: set it again if you still want it."
            return r
        except Exception as e:
            log.exception("spread close failed")
            if sent["v"]:
                self.book.hold(sid, f"error after sending the close ({type(e).__name__}): check thinkorswim, then Resolve")
                return {"ok": False, "spread_id": sid, "status": "UNKNOWN",
                        "error": "Error after sending the close. It MAY be live: check thinkorswim."}
            return {"ok": False, "spread_id": sid, "error": f"Not sent: {e}"[:300]}
        finally:
            self.t._send_lock.release()

    def _target_now(self, client, sp: dict) -> tuple[dict | None, int, str | None]:
        """The spread's target read now: (order or None, contracts it can still close, refusal). A final
        target's fills are recorded; a REPLACED one or fills Schwab has not fully listed refuse."""
        oid = sp.get("target_order_id")
        if oid is None:
            return None, 0, None
        r = self.t._logged("order_details", lambda: client.order_details(self.t._hash, oid))
        if r.status_code != 200:
            return None, 0, f"Could not read the target {oid} (HTTP {r.status_code}): nothing was sent."
        o = r.json()
        bad = _not_settled(sp, "target", o)
        if bad:
            return o, 0, bad
        self.book._save_fills(sp["spread_id"], "target", o)      # what it filled so far is no longer held
        if o.get("status") in TERMINAL:
            return o, 0, None
        rem = float(o.get("quantity") or 0) - float(o.get("filledQuantity") or 0)
        return o, max(int(round(rem)), 0), None

    @staticmethod
    def _want_qty(req: dict, default: int) -> int | None:
        q = req.get("qty")
        if q in (None, "", "all"):
            return default
        try:
            return int(str(q).strip())
        except ValueError:
            return None

    def _close_locked(self, sp: dict, req: dict, sent: dict) -> dict:
        sid = sp["spread_id"]
        moved_rest = None
        if req.get("reprice") and sp["state"] == "CLOSING" and not sp.get("hold"):
            # Move a working close: cancel it, and only once Schwab confirms it CANCELED with nothing
            # filled, send the new one through every check below. Never two closes working at once. It
            # closes what the old one had left (a partial close stays partial).
            c = self._cancel_locked(sp, only="close")
            if not c["ok"]:
                return {"ok": False, "spread_id": sid, "status": c.get("status"),
                        "error": f"The working close was not replaced: {c['error']}"}
            sent["close_canceled"] = sp["close_order_id"]
            moved_rest = int(round(float(c.get("want") or 0) - float(c.get("filled") or 0)))
            self.book.set_state(sid, "OPEN", "close canceled to move its price")
            sp = self.book.get(sid)
            if moved_rest < 1 and req.get("qty") is None:
                return {"ok": False, "spread_id": sid, "error": "The old close had nothing left to move: check the spread."}
        if sp["state"] != "OPEN" or sp.get("hold"):
            return {"ok": False, "spread_id": sid, "error": f"Only an OPEN spread can be closed (this one is "
                                                            f"{sp['state']})."}
        exp = date.fromisoformat(sp["expiration"]) if sp.get("expiration") else None
        if exp and datetime.now(ET).date() > exp:
            return {"ok": False, "spread_id": sid, "error": "Past expiration: check settlement in thinkorswim."}
        exp_limit = _d(req.get("expected_limit"))
        if exp_limit is None or exp_limit <= 0:
            return {"ok": False, "spread_id": sid, "error": "the close is missing its displayed limit; click again"}
        p = self.close_plan(sid, "limit", str(exp_limit), fresh=True)
        if not p["ok"]:
            return {"ok": False, "spread_id": sid, "error": "; ".join(p["errors"])}
        client = self.t._ensure()
        syms = {l["symbol"] for l in sp["legs"]}
        tgt, rest_t, bad = self._target_now(client, sp)
        if bad:
            return {"ok": False, "spread_id": sid, "error": bad}
        sp = self.book.get(sid)
        # Close what the ledger holds (a partly filled entry holds less than the ticket's quantity), or part
        # of it, and only when the account holds exactly that.
        units = self.book.held_units(sp)
        if units < 1e-9 and tgt is not None and float(tgt.get("filledQuantity") or 0) > 0:
            return {"ok": False, "spread_id": sid, "status": "FILLED",
                    "error": "The target filled before the close: nothing is left to close."}
        if units < 1 or abs(units - round(units)) > 1e-9:
            return {"ok": False, "spread_id": sid, "error": f"The ledger holds {units:g} spreads: nothing to close "
                                                            "as one order. Check thinkorswim."}
        units = int(round(units))
        qty = self._want_qty(req, min(moved_rest, units) if moved_rest is not None and req.get("qty") is None else units)
        if qty is None or not (1 <= qty <= units):
            return {"ok": False, "spread_id": sid, "error": f"Close 1 to {units} spreads."}
        done, retarget = frozenset({sent["close_canceled"]} if sent.get("close_canceled") else ()), None
        if tgt is not None:
            done = done | {sp["target_order_id"]}
        if rest_t and qty + rest_t > units:
            # The target and this close would cover more than is held: cancel the target first, and
            # confirm it (Schwab must show it final). Whatever it filled is recorded and not closed again;
            # a partial close puts the same target back on what stays open.
            c = self._cancel_locked(sp, only="target")
            if not c["ok"] and c.get("working") is not False and c.get("status") != "FILLED":
                return {"ok": False, "spread_id": sid,
                        "error": f"The target was not cancelled, so nothing was closed: {c['error']}"}
            sp = self.book.get(sid)
            left = self.book.held_units(sp)
            if left < 1e-9:
                return {"ok": False, "spread_id": sid, "status": "FILLED",
                        "error": "The target filled before the close: nothing is left to close."}
            if c["ok"]:
                sent["target_canceled"] = True
                self.book.set_state(sid, "OPEN", "target canceled to close")
            units = int(round(left))
            qty = min(qty, units)
            if units - qty > 0 and tgt.get("price") is not None:
                retarget = (_d(tgt["price"]), units - qty)
        # (A target that, with this close, never covers more than is held keeps working.)
        held = {(x.get("instrument") or {}).get("symbol"): float(x.get("longQuantity") or 0) - float(x.get("shortQuantity") or 0)
                for x in self.t._positions(client)}
        for l in sp["legs"]:
            want = (1 if l["side"] == "LONG" else -1) * abs(l["ratio"]) * units
            if abs(held.get(l["symbol"], 0.0) - want) > 1e-9:
                return {"ok": False, "spread_id": sid,
                        "error": f"The account holds {held.get(l['symbol'], 0.0):+g} {l['symbol'].strip()}, the spread "
                                 f"{want:+g}: not closed. Check thinkorswim."}
        working = self._working_on(client, syms, exclude=done)
        if working:
            return {"ok": False, "spread_id": sid, "error": f"Working order(s) {', '.join(map(str, working))} touch "
                                                            "this spread's contracts: cancel them first."}
        debit = sp["kind"].endswith("DEBIT")
        if req.get("price_at_send"):
            # The stop's close: natural read now (after the cancels), plus its tolerance, never past max loss.
            exp_limit = self._stop_price(sid, debit)
            if exp_limit is None:
                return {"ok": False, "spread_id": sid, "error": "no valid quote to price the close; nothing was sent"}
        order = close_order(sp["legs"], debit, exp_limit, qty)
        pv = self._preview(client, order)
        if not pv["ok"]:
            return {"ok": False, "spread_id": sid, "error": (f"Schwab's preview refused the close: {pv['error']}"
                                                             if pv.get("rejected") else pv["error"]) + ". Nothing was sent."}
        log.warning("CLOSE spread %s %s x%s of %s @ %s", sid, sp["kind"], qty, units, exp_limit)
        self.t.store.log_action("spread_close", sp["underlying"], None, "SENDING", sid)
        # Held BEFORE the POST: if the app stops between Schwab's answer and recording it, the spread
        # stays NEEDS_ACTION (never OPEN, never closed twice) until Resolve.
        self.book.hold(sid, "close being sent: if this stays, check thinkorswim, then Resolve")
        sent["v"] = True
        try:
            r = self.t._logged("place_order", lambda: client.place_order(self.t._hash, order))
        except Exception as e:
            self.book.hold(sid, f"close sent, no answer from Schwab ({type(e).__name__}): check thinkorswim, then Resolve")
            return {"ok": False, "spread_id": sid, "status": "UNKNOWN",
                    "error": "No answer from Schwab. The close MAY be live: check thinkorswim."}
        if r.status_code in (200, 201):
            tail = r.headers.get("Location", "").rstrip("/").split("/")[-1]
            oid = int(tail) if tail.isdigit() else None
            if oid is None:
                self.book.hold(sid, "close accepted without an order id: check thinkorswim, then Resolve")
                return {"ok": False, "spread_id": sid, "status": "UNKNOWN",
                        "error": "Schwab accepted the close but gave no order id. Check thinkorswim."}
            self.book.attach_order(sid, "close", oid)
            self.book.clear_hold(sid)
            self.book.set_state(sid, "CLOSING", "close sent")
            self.t.store.log_action("spread_close", sp["underlying"], oid, "ACCEPTED", sid)
            out = {"ok": True, "spread_id": sid, "order_id": oid, "limit": _f(exp_limit), "qty": qty, "of": units}
            if retarget:
                tsent = {"v": False, "old": False}
                try:
                    rt = self._send_target(client, self.book.get(sid), retarget[0], retarget[1], tsent,
                                           ("CLOSING", "close sent, target back on the rest"))
                except Exception as e:
                    log.exception("putting the target back failed")
                    if tsent["v"]:
                        self.book.hold(sid, f"target sent after a partial close, unclear ({type(e).__name__}): check "
                                            "thinkorswim, then Resolve")
                    rt = {"ok": False, "error": f"{type(e).__name__}" + (": it MAY be live, check thinkorswim"
                                                                         if tsent["v"] else "")}
                out["target"] = rt
                if not rt["ok"]:
                    out["warning"] = f"The close was sent, but the target was NOT put back: {rt['error']}"
            return out
        if r.status_code == 429 or 400 <= r.status_code < 500:
            # Schwab refused it: nothing was placed, the spread is open as before.
            sent["v"] = False
            self.book.clear_hold(sid)
            self.book.set_state(sid, "OPEN", "close not placed")
        if r.status_code == 429:
            return {"ok": False, "spread_id": sid, "status": "RATE_LIMITED",
                    "error": "Schwab throttled the close (HTTP 429): nothing was placed. Click again to retry."}
        if 400 <= r.status_code < 500:
            self.t.store.log_action("spread_close", sp["underlying"], None, "REJECTED", f"HTTP {r.status_code}")
            return {"ok": False, "spread_id": sid, "status": "REJECTED",
                    "error": f"Schwab rejected the close (HTTP {r.status_code}): {plain_refusal(r.text[:300])}"}
        self.book.hold(sid, f"close answered HTTP {r.status_code}: check thinkorswim, then Resolve")
        return {"ok": False, "spread_id": sid, "status": "UNKNOWN",
                "error": f"Schwab answered HTTP {r.status_code}. The close MAY be live: check thinkorswim."}

    # ---- profit target (slice 3) --------------------------------------------------------------

    def target_plan(self, spread_id: str, pct=None, price=None) -> dict:
        """The resting target a click would send: a paired close at `pct` of the max gain (rounded on
        the spread's grid in your favor) or at a typed net price, with what it banks. Display only:
        the price comes from the entry fills, the grid from the poller's leg quotes (no request)."""
        sp = self.book.get(spread_id) if self.book else None
        if not sp:
            return {"ok": False, "errors": ["No such spread."]}
        t = sp.get("ticket") or {}
        debit = sp["kind"].endswith("DEBIT")
        width = _d(t.get("width"))
        entry = fill_net(self.t.store.get_order(sp["entry_order_id"]), sp) if sp["entry_order_id"] else None
        basis = "fill"
        if entry is None:
            entry, basis = _d(t.get("entry_limit")), "limit"
        out = {"ok": False, "errors": [], "spread_id": spread_id, "entry": _f(entry), "basis": basis,
               "side": "sell" if debit else "buy", "held": self.book.held_units(sp)}
        if entry is None or width is None:
            out["errors"].append("the ticket record is missing")
            return out
        near = next((l for l in sp["legs"] if (l["side"] == "LONG") == debit), sp["legs"][0])
        far = next(l for l in sp["legs"] if l is not near)
        qn, qf = self._held_quote(near["symbol"]), self._held_quote(far["symbol"])
        step = spread_step(sp["underlying"], qn, qf)
        gain1, loss1 = (width - entry, entry) if debit else (entry, width - entry)
        if price is not None and str(price).strip() != "":
            px = _d(price)
            if px is None or px <= 0 or px % step:
                out["errors"].append(f"type a target in {step:.2f} steps")
                return out
        else:
            f = _d(pct)
            if f is None or not (0 < f < 1):
                out["errors"].append("pick a % of the max gain")
                return out
            if basis != "fill":
                out["errors"].append("Schwab has not listed the fill prices yet, so a % of the max gain is unknown: "
                                     "type a price, or wait a moment")
                return out
            # At least that % of the max gain: buy back at or below, sell at or above.
            px = on_step(entry + gain1 * f, step, up=True) if debit else on_step(entry - gain1 * f, step, up=False)
        profit1 = px - entry if debit else entry - px
        if px < step or px > width or profit1 <= 0:
            out["errors"].append(f"a target must be {'above' if debit else 'below'} the entry {entry:.2f} and "
                                 f"{'at most the width ' + format(width, '.2f') if debit else 'at least ' + format(step, '.2f')}")
            return out
        held = Decimal(str(out["held"]))
        n = nets(qn, qf)
        natural = (n["sell"] if debit else n["buy"]) if n else None
        out.update({"ok": True, "limit": _f(px), "step": _f(step), "pct": float(profit1 / gain1) if gain1 else None,
                    "r": float(profit1 / loss1) if loss1 else None, "gross": _f(profit1 * MULT * held),
                    "natural": _f(natural), "mid": _f(n["mid"]) if n else None})
        if natural is not None and ((debit and px <= natural) or (not debit and px >= natural)):
            out["warnings"] = [f"at the market ({natural:.2f}) now: it fills at once"]
        return out

    def stop_plan(self, spread_id: str, r=None, price=None) -> dict:
        """The watch-only stop a click would set: at `r` of the max loss (1R) past the entry fill, on
        the spread's grid, tighter rather than looser, or at a typed net price. Display only."""
        sp = self.book.get(spread_id) if self.book else None
        if not sp:
            return {"ok": False, "errors": ["No such spread."]}
        t = sp.get("ticket") or {}
        debit = sp["kind"].endswith("DEBIT")
        width = _d(t.get("width"))
        entry = fill_net(self.t.store.get_order(sp["entry_order_id"]), sp) if sp["entry_order_id"] else None
        out = {"ok": False, "errors": [], "spread_id": spread_id, "entry": _f(entry), "side": "sell" if debit else "buy",
               "held": self.book.held_units(sp)}
        if entry is None or width is None:
            out["errors"].append("Schwab has not listed the fill prices yet: wait a moment")
            return out
        near = next((l for l in sp["legs"] if (l["side"] == "LONG") == debit), sp["legs"][0])
        far = next(l for l in sp["legs"] if l is not near)
        step = spread_step(sp["underlying"], self._held_quote(near["symbol"]), self._held_quote(far["symbol"]))
        loss1 = entry if debit else width - entry
        if price is not None and str(price).strip() != "":
            px = _d(price)
            if px is None or px < 0 or px % step:
                out["errors"].append(f"type a stop in {step:.2f} steps")
                return out
        else:
            f = _d(r)
            if f is None or not (0 < f <= 1):
                out["errors"].append("pick how much of the max loss (R)")
                return out
            # Tighter, never looser: a credit spread's stop rounds down, a debit spread's up.
            px = on_step(entry - loss1 * f, step, up=True) if debit else on_step(entry + loss1 * f, step, up=False)
        lose1 = entry - px if debit else px - entry
        if lose1 <= 0 or px < 0 or px > width:
            out["errors"].append(f"a stop must be {'below' if debit else 'above'} the entry {entry:.2f} "
                                 f"and {'at least 0' if debit else 'at most the width ' + format(width, '.2f')}")
            return out
        held = Decimal(str(out["held"]))
        out.update({"ok": True, "limit": _f(px), "step": _f(step), "r": float(lose1 / loss1) if loss1 else None,
                    "gross": _f(-lose1 * MULT * held)})
        return out

    def set_stop(self, req: dict) -> dict:
        """Watch a stop on an OPEN spread. With `send` (the user's opt-in) a fire sends ONE close of all it
        holds (see `auto_stop`); without it nothing is ever sent."""
        sid = str(req.get("spread_id") or "")
        sp = self.book.get(sid) if self.book else None
        if not sp:
            return {"ok": False, "error": "No such spread."}
        if sp["state"] != "OPEN":
            return {"ok": False, "error": f"A stop needs an OPEN spread (this one is {sp['state']})."}
        p = self.stop_plan(sid, price=req.get("expected_price"))
        if not p["ok"]:
            return {"ok": False, "error": "; ".join(p["errors"])}
        send = req.get("send") is True
        if not self.book.stops.set(sid, float(p["limit"]), p["r"], send=send):
            return {"ok": False, "error": "The stop's close is being sent right now: too late to change it. "
                                          "Check thinkorswim."}
        return {"ok": True, "spread_id": sid, "price": p["limit"], "r": p["r"], "send": send}

    STOP_TOLERANCE = 2         # grid steps past natural a stop's close may fill at

    def auto_stop(self, spread_id: str) -> dict:
        """A stop with Send on just fired: ONE close of everything the spread holds, at natural plus two
        grid steps, never past the max loss (a credit spread's width, a debit spread's minimum). Claimed
        once (never retried, even after a refusal); a working target or close is cancelled first by the
        same Close path a click uses; an unclear answer holds the spread (NEEDS_ACTION)."""
        stops = self.book.stops
        claim = stops.claim_send(spread_id)
        if not claim:
            return {"ok": False, "error": "already sent or not armed"}
        r = {"ok": False, "error": "not sent"}
        sent = {"v": False}
        locked = self.t._send_lock.acquire(timeout=30)     # a click's Move close with a target can take a while
        try:
            if not locked:
                r = {"ok": False, "error": "another order action was running; nothing was sent"}
            elif not stops.start_send(spread_id, claim):
                # Removed, set again or switched to watch-only while this send waited for the lock:
                # that consent is gone, nothing is sent. From here on a removal is refused (too late).
                r = {"ok": False, "error": "the stop was removed or changed before sending; nothing was sent"}
            elif not market_open():
                r = {"ok": False, "error": "outside regular hours; nothing was sent"}
            else:
                sp = self.book.get(spread_id)
                if not sp or sp["state"] not in ("OPEN", "CLOSING") or sp.get("hold"):
                    r = {"ok": False, "error": f"the spread is {sp['state'] if sp else 'gone'}; check it yourself"}
                else:
                    px = self._stop_price(spread_id, sp["kind"].endswith("DEBIT"))
                    if px is None:
                        r = {"ok": False, "error": "no valid quote to price the close; nothing was sent"}
                    else:
                        log.warning("AUTO STOP spread %s: close all near %s (repriced at send)", spread_id, px)
                        r = self._close_locked(sp, {"spread_id": spread_id, "expected_limit": str(px), "qty": "all",
                                                    "reprice": sp["state"] == "CLOSING", "price_at_send": True}, sent)
        except Exception as e:
            log.exception("auto stop failed")
            if sent["v"]:
                self.book.hold(spread_id, f"error after sending the stop's close ({type(e).__name__}): check "
                                          "thinkorswim, then Resolve")
                r = {"ok": False, "status": "UNKNOWN", "error": "error after sending: the close MAY be live, check thinkorswim"}
            else:
                r = {"ok": False, "error": f"not sent ({type(e).__name__})"}
        finally:
            if locked:
                self.t._send_lock.release()
        # Three outcomes, never mixed up: a close that MAY be live must never read "close it yourself".
        # The stored text says what happened only; the page adds advice from the spread's CURRENT state
        # (a spread closed meanwhile must never be told to close again: that would open the opposite spread).
        if r["ok"]:
            outcome, text = "sent", f"close order {r['order_id']} for {r['qty']} at {r['limit']}"
        elif sent["v"] or r.get("status") == "UNKNOWN":
            outcome, text = "unclear", (r.get("error") or "no clear answer") + "; do NOT close again before checking"
        elif r.get("status") == "FILLED":
            outcome, text = "closed", r.get("error") or "already closed"
        else:
            outcome, text = "refused", r.get("error") or "not sent"
            gone = [w for w, k in (("the working close", "close_canceled"), ("the target", "target_canceled")) if sent.get(k)]
            if gone:
                text += f" ({' and '.join(gone)} had been cancelled: the spread has no exit order now)"
        stops.record_send(spread_id, outcome, text, claim)
        return r

    def _stop_price(self, spread_id: str, debit: bool) -> Decimal | None:
        """The stop's close price: natural read now, plus STOP_TOLERANCE grid steps, never past the max
        loss (a credit spread's width, a debit spread's smallest step)."""
        p = self.close_plan(spread_id, "natural", fresh=True)
        if not p["ok"]:
            return None
        nat, step, width = Decimal(p["natural"]), Decimal(p["step"]), Decimal(p["width"])
        px = max(nat - self.STOP_TOLERANCE * step, step) if debit else min(nat + self.STOP_TOLERANCE * step, width)
        return on_step(px, step, up=not debit)

    def set_target(self, req: dict) -> dict:
        """Send (or move) the spread's GTC profit target: one paired net-limit close of what is held,
        after Schwab's preview accepts it. Moving cancels the working target first and confirms it.
        Never resent: an unclear outcome holds the spread (NEEDS_ACTION) until Resolve."""
        sid = str(req.get("spread_id") or "")
        sp = self.book.get(sid) if self.book else None
        if not sp:
            return {"ok": False, "error": "No such spread."}
        if not market_open():
            return {"ok": False, "spread_id": sid, "error": regular_hours_text()}
        if not self.t._send_lock.acquire(blocking=False):
            return {"ok": False, "spread_id": sid, "error": "Another order action is in progress."}
        sent = {"v": False, "old": None}
        try:
            return self._target_locked(self.book.get(sid), req, sent)
        except Exception as e:
            log.exception("spread target failed")
            if sent["v"]:
                self.book.hold(sid, f"error after sending the target ({type(e).__name__}): check thinkorswim, then Resolve")
                return {"ok": False, "spread_id": sid, "status": "UNKNOWN",
                        "error": "Error after sending the target. It MAY be live: check thinkorswim."}
            return {"ok": False, "spread_id": sid, "error": f"Not sent: {e}"[:300] + (
                " The old target was already cancelled." if sent["old"] else "")}
        finally:
            self.t._send_lock.release()

    def _target_locked(self, sp: dict, req: dict, sent: dict) -> dict:
        sid = sp["spread_id"]
        if sp["state"] != "OPEN" or sp.get("hold"):
            return {"ok": False, "spread_id": sid, "error": f"A target needs an OPEN spread (this one is {sp['state']})."}
        exp = date.fromisoformat(sp["expiration"]) if sp.get("expiration") else None
        if exp and datetime.now(ET).date() > exp:
            return {"ok": False, "spread_id": sid, "error": "Past expiration: check settlement in thinkorswim."}
        exp_limit = _d(req.get("expected_limit"))
        if exp_limit is None or exp_limit <= 0:
            return {"ok": False, "spread_id": sid, "error": "the target is missing its displayed price; click again"}
        p = self.target_plan(sid, price=str(exp_limit))
        if not p["ok"]:
            return {"ok": False, "spread_id": sid, "error": "; ".join(p["errors"])}
        client = self.t._ensure()
        done, old = frozenset(), sp.get("target_order_id")
        if old is not None:
            cur, rest, bad = self._target_now(client, sp)
            if bad:
                return {"ok": False, "spread_id": sid, "error": bad}
            units_now = self.book.held_units(self.book.get(sid))
            if rest and cur and cur.get("price") is not None and _d(cur["price"]) == exp_limit and rest >= units_now - 1e-9:
                return {"ok": False, "spread_id": sid, "error": f"The target already works at {exp_limit:.2f}: nothing to move."}
            c = self._cancel_locked(sp, only="target")
            if not c["ok"] and c.get("working") is not False and c.get("status") != "FILLED":
                return {"ok": False, "spread_id": sid, "error": f"The working target was not moved: {c['error']}"}
            done = frozenset({old})
            sent["old"] = c["ok"]
            sp = self.book.get(sid)
            if self.book.held_units(sp) < 1e-9:
                return {"ok": False, "spread_id": sid, "status": "FILLED",
                        "error": "The old target filled: nothing is left to target."}
            if c["ok"]:
                self.book.set_state(sid, "OPEN", "target canceled to move it")
        moved = " The old target was cancelled." if sent["old"] else ""
        units = self.book.held_units(sp)
        if units < 1 or abs(units - round(units)) > 1e-9:
            return {"ok": False, "spread_id": sid, "error": f"The ledger holds {units:g} spreads: no target as one "
                                                            f"order. Check thinkorswim.{moved}"}
        units = int(round(units))
        syms = {l["symbol"] for l in sp["legs"]}
        held = {(x.get("instrument") or {}).get("symbol"): float(x.get("longQuantity") or 0) - float(x.get("shortQuantity") or 0)
                for x in self.t._positions(client)}
        for l in sp["legs"]:
            want = (1 if l["side"] == "LONG" else -1) * abs(l["ratio"]) * units
            if abs(held.get(l["symbol"], 0.0) - want) > 1e-9:
                return {"ok": False, "spread_id": sid,
                        "error": f"The account holds {held.get(l['symbol'], 0.0):+g} {l['symbol'].strip()}, the spread "
                                 f"{want:+g}: no target sent. Check thinkorswim.{moved}"}
        working = self._working_on(client, syms, exclude=done)
        if working:
            return {"ok": False, "spread_id": sid, "error": f"Working order(s) {', '.join(map(str, working))} touch "
                                                            f"this spread's contracts: no target sent.{moved}"}
        return self._send_target(client, sp, exp_limit, units, sent, ("OPEN", f"target sent at {exp_limit:.2f}"), moved)

    def _send_target(self, client, sp: dict, price: Decimal, units: int, sent: dict, after: tuple[str, str],
                      moved: str = "") -> dict:
        """Preview and send one GTC paired close (the target) of `units` at `price`; the spread then shows
        `after` (state, detail). Held before the POST: an unclear answer never allows a second target."""
        sid = sp["spread_id"]
        debit = sp["kind"].endswith("DEBIT")
        order = close_order(sp["legs"], debit, price, units, duration="GOOD_TILL_CANCEL")
        pv = self._preview(client, order)
        if not pv["ok"]:
            return {"ok": False, "spread_id": sid, "error": (f"Schwab's preview refused the target: {pv['error']}"
                                                             if pv.get("rejected") else pv["error"]) + f". Nothing was sent.{moved}"}
        log.warning("TARGET spread %s %s x%s @ %s GTC", sid, sp["kind"], units, price)
        self.t.store.log_action("spread_target", sp["underlying"], None, "SENDING", sid)
        self.book.hold(sid, "target being sent: if this stays, check thinkorswim, then Resolve")
        sent["v"] = True
        try:
            r = self.t._logged("place_order", lambda: client.place_order(self.t._hash, order))
        except Exception as e:
            self.book.hold(sid, f"target sent, no answer from Schwab ({type(e).__name__}): check thinkorswim, then Resolve")
            return {"ok": False, "spread_id": sid, "status": "UNKNOWN",
                    "error": "No answer from Schwab. The target MAY be live: check thinkorswim."}
        if r.status_code in (200, 201):
            tail = r.headers.get("Location", "").rstrip("/").split("/")[-1]
            oid = int(tail) if tail.isdigit() else None
            if oid is None:
                self.book.hold(sid, "target accepted without an order id: check thinkorswim, then Resolve")
                return {"ok": False, "spread_id": sid, "status": "UNKNOWN",
                        "error": "Schwab accepted the target but gave no order id. Check thinkorswim."}
            self.book.attach_order(sid, "target", oid)
            self.book.clear_hold(sid)
            self.book.set_state(sid, *after)
            self.t.store.log_action("spread_target", sp["underlying"], oid, "ACCEPTED", sid)
            return {"ok": True, "spread_id": sid, "order_id": oid, "limit": _f(price), "qty": units,
                    "moved": bool(sent.get("old"))}
        sent["v"] = False
        if r.status_code == 429 or 400 <= r.status_code < 500:
            self.book.clear_hold(sid)
            self.book.set_state(sid, after[0], "target not placed")
            self.t.store.log_action("spread_target", sp["underlying"], None, "REJECTED", f"HTTP {r.status_code}")
            why = ("Schwab throttled the target (HTTP 429): nothing was placed. Click again to retry." if r.status_code == 429
                   else f"Schwab rejected the target (HTTP {r.status_code}): {plain_refusal(r.text[:300])}")
            return {"ok": False, "spread_id": sid, "status": "RATE_LIMITED" if r.status_code == 429 else "REJECTED",
                    "error": why + moved}
        self.book.hold(sid, f"target answered HTTP {r.status_code}: check thinkorswim, then Resolve")
        return {"ok": False, "spread_id": sid, "status": "UNKNOWN",
                "error": f"Schwab answered HTTP {r.status_code}. The target MAY be live: check thinkorswim."}

    def cancel_target(self, spread_id: str) -> dict:
        """Cancel the spread's working target (only removes an exit order: no arming needed)."""
        sp = self.book.get(spread_id) if self.book else None
        if not sp:
            return {"ok": False, "error": "No such spread."}
        if not self.t._send_lock.acquire(blocking=False):
            return {"ok": False, "error": "Another order action is in progress."}
        try:
            c = self._cancel_locked(sp, only="target")
            if c["ok"] and sp["state"] == "OPEN":
                self.book.set_state(spread_id, "OPEN", "target canceled")
            return c
        except Exception as e:
            log.exception("spread target cancel failed")
            return {"ok": False, "spread_id": spread_id, "error": f"Cancel failed: {e}"[:300]}
        finally:
            self.t._send_lock.release()
