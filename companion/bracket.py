"""One-click entry with defined risk: sizing and the Schwab bracket order. Pure, no I/O.

You give symbol, side, stop and risk $, and where to enter: a LIMIT at the mid, the bid or the
ask (literal prices; for a long the bid rests and the ask takes, for a short the reverse), a
MARKET order, or "cross" (limit 0.05% through the market, the original default). Shares are sized
from the limit, so any fill at or better than it keeps the planned risk; a market entry is sized
from the ask (bid for a short) plus 0.05% and its risk is an estimate. One Schwab order carries everything: a TRIGGER parent (the entry)
whose child OCO holds the STOP (market on trigger) and the LIMIT target at R x the risk per share.
Schwab holds the exits, so protection does not depend on the companion noticing the fill.

Planned risk is not a maximum loss: a stop-market can fill past its price on a gap or halt.

The stop can also be a Schwab-held TRAILING_STOP (stop_type "trail" = R / $ / % behind). Shares are
still sized from the typed stop; a trail wider than the typed stop starts further away, so the plan
warns. stop_type "autobe" sends the same fixed stop as "fixed"; the companion then moves it to
break-even once, when the mark first touches +1R (the poller watches it; be_trigger shows where).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_UP, Decimal

ENTRY_OFFSET = Decimal("0.0005")     # 0.05% through the ask / bid
MAX_SHARES = 10_000
MAX_NOTIONAL = Decimal("250000")
MIN_STOP_TICKS = 2


def tick(price: Decimal) -> Decimal:
    return Decimal("0.01") if price >= 1 else Decimal("0.0001")


def _to_tick(price: Decimal, rounding: str) -> Decimal:
    t = tick(price)
    return (price / t).to_integral_value(rounding=rounding) * t


def fmt(price: Decimal) -> str:
    return f"{price:.2f}" if price >= 1 else f"{price:.4f}"


@dataclass
class Plan:
    symbol: str
    side: str                      # LONG | SHORT
    stop: Decimal
    risk: Decimal                  # $ the user chose
    r_multiple: Decimal
    bid: Decimal
    ask: Decimal
    limit: Decimal = Decimal(0)    # entry limit price
    qty: int = 0
    per_share: Decimal = Decimal(0)
    target: Decimal = Decimal(0)
    planned_risk: Decimal = Decimal(0)
    notional: Decimal = Decimal(0)
    # Two-target entry: qty1 shares exit at target1 (t1_r), the rest (runner) at target.
    split: Decimal = Decimal(0)    # fraction of shares on target 1; 0 = single target
    t1_r: Decimal = Decimal(0)
    qty1: int = 0
    target1: Decimal = Decimal(0)
    entry: str = "cross"                  # mid | bid | ask | market | cross
    stop_type: str = "fixed"              # fixed | autobe | trail
    trail: dict | None = None             # {"offset": Decimal, "link": VALUE|PERCENT, "basis": MARK|...}
    trail_start: Decimal = Decimal(0)     # where the trail starts if the entry fills at the limit
    be_trigger: Decimal = Decimal(0)      # autobe: +1R from the limit (the real one uses the fill)
    warnings: list[str] = field(default_factory=list)
    target_price: Decimal | None = None   # the user typed the runner's price (overrides R)
    t1_price: Decimal | None = None       # and/or target 1's price
    errors: list[str] = field(default_factory=list)

    @property
    def market(self) -> bool:
        return self.entry == "market"

    @property
    def ok(self) -> bool:
        return not self.errors and self.qty > 0

    @property
    def two_targets(self) -> bool:
        return self.qty1 > 0

    def _r_of(self, price: Decimal) -> Decimal:
        return (abs(price - self.limit) / self.per_share).quantize(Decimal("0.01")) if self.per_share else Decimal(0)

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "side": self.side, "stop": fmt(self.stop), "risk": float(self.risk),
            "r_multiple": float(self.r_multiple), "bid": float(self.bid), "ask": float(self.ask),
            "limit": fmt(self.limit) if self.limit else None, "qty": self.qty,
            "per_share": float(self.per_share), "target": fmt(self.target) if self.target else None,
            "planned_risk": float(self.planned_risk), "notional": float(self.notional),
            "split": float(self.split), "t1_r": float(self.t1_r), "qty1": self.qty1,
            "qty2": self.qty - self.qty1 if self.qty1 else 0,
            "target1": fmt(self.target1) if self.target1 else None,
            "entry": self.entry, "market": self.market, "stop_type": self.stop_type,
            "trail": ({"offset": float(self.trail["offset"]), "link": self.trail["link"], "basis": self.trail["basis"]}
                      if self.trail else None),
            "trail_start": fmt(self.trail_start) if self.trail_start else None, "warnings": self.warnings,
            "be_trigger": fmt(self.be_trigger) if self.be_trigger else None,
            "target_r": float(self._r_of(self.target)) if self.target else None,
            "target1_r": float(self._r_of(self.target1)) if self.target1 else None,
            "errors": self.errors, "ok": self.ok,
        }


def _price(v) -> Decimal | None:
    """Blank means "use R"; anything else must be a number (an unparseable value becomes NaN,
    which the finite check below turns into an error instead of silently falling back to R)."""
    if v is None or str(v).strip() == "":
        return None
    try:
        return Decimal(str(v).strip())
    except Exception:
        return Decimal("NaN")


TRAIL_BASES = ("MARK", "LAST", "BID", "ASK")


def trail_spec(unit: str, value, per_share: Decimal | None, basis: str = "MARK") -> tuple[dict | None, str | None]:
    """Schwab trailing-stop fields from a distance in R (needs the risk per share), $ or %.
    Returns (spec, error)."""
    unit = {"$": "USD", "%": "PCT", "PERCENT": "PCT", "DOLLAR": "USD"}.get((unit or "").upper().strip(), (unit or "").upper().strip())
    basis = (basis or "MARK").upper()
    if basis not in TRAIL_BASES:
        return None, f"trail basis must be one of {', '.join(TRAIL_BASES)}"
    try:
        v = Decimal(str(value))
    except Exception:
        return None, "trail distance is not a number"
    if not v.is_finite() or v <= 0:
        return None, "trail distance must be above 0"
    if unit == "R":
        if not per_share:
            return None, "a trail in R needs the frozen 1R of a companion entry: use $ or %"
        return {"offset": _to_tick(per_share * v, ROUND_HALF_UP), "link": "VALUE", "basis": basis}, None
    if unit == "USD":
        return {"offset": _to_tick(v, ROUND_HALF_UP), "link": "VALUE", "basis": basis}, None
    if unit == "PCT":
        if v >= 50:
            return None, "trail percent must be below 50"
        return {"offset": v.quantize(Decimal("0.01")), "link": "PERCENT", "basis": basis}, None
    return None, "trail unit must be R, $ or %"


def trail_level(from_price: Decimal, spec: dict, side: str) -> Decimal:
    """Where a trailing stop sits when the best price so far is from_price."""
    off = (from_price * spec["offset"] / 100) if spec["link"] == "PERCENT" else spec["offset"]
    raw = from_price - off if side == "LONG" else from_price + off
    return _to_tick(raw, ROUND_HALF_UP)


def stop_leg(spec, legs: list[dict], duration: str = "DAY") -> dict:
    """The stop child of an exit: a STOP at a price (Decimal) or a Schwab TRAILING_STOP (dict)."""
    if isinstance(spec, dict):
        off = spec["offset"]
        return {"orderStrategyType": "SINGLE", "orderType": "TRAILING_STOP", "session": "NORMAL",
                "duration": duration, "stopPriceLinkBasis": spec["basis"], "stopPriceLinkType": spec["link"],
                "stopPriceOffset": float(off), "orderLegCollection": legs}
    return {"orderStrategyType": "SINGLE", "orderType": "STOP", "session": "NORMAL", "duration": duration,
            "stopPrice": fmt(spec), "orderLegCollection": legs}


def plan(symbol: str, side: str, stop, risk, r_multiple, bid, ask, split=0, t1_r=1, entry="cross",
         target_price=None, t1_price=None, stop_type="fixed", trail_unit="R", trail_value=1,
         trail_basis="MARK", limit_override=None) -> Plan:
    """Size a bracket. Never moves the user's prices: an off-tick stop or target is an error, not rounded.

    split > 0 makes it a two-target entry: floor(qty x split) shares exit at target 1, the runner
    at the target, each half with its own stop (Schwab OT2OCO). Each target is at R x the risk
    per share, or at the price the user typed (target_price / t1_price), which wins over R.

    limit_override sizes from a given entry price instead of the quote (the click locks the price
    the ticket showed); the stop is still checked against the current bid / ask.
    """
    side = side.upper()
    p = Plan(symbol.upper().strip(), side, Decimal(str(stop)), Decimal(str(risk)),
             Decimal(str(r_multiple or 0)), Decimal(str(bid or 0)), Decimal(str(ask or 0)),
             split=Decimal(str(split or 0)), t1_r=Decimal(str(t1_r or 0)), entry=(entry or "cross").lower(),
             target_price=_price(target_price), t1_price=_price(t1_price),
             stop_type=(stop_type or "fixed").lower())
    if side not in ("LONG", "SHORT"):
        p.errors.append("side must be LONG or SHORT")
        return p
    if p.entry not in ("mid", "bid", "ask", "market", "cross"):
        p.errors.append("entry must be mid, bid, ask, market or cross")
        return p
    typed = [v for v in (p.target_price, p.t1_price) if v is not None]
    if any(not v.is_finite() for v in typed):
        p.errors.append("a typed target price is not a number")
        return p
    if not all(v.is_finite() for v in (p.stop, p.risk, p.r_multiple, p.bid, p.ask, p.split, p.t1_r)):
        p.errors.append("all numbers must be finite")
        return p
    if p.bid <= 0 or p.ask <= 0 or p.ask < p.bid:
        p.errors.append("no valid bid/ask quote")
        return p
    if p.risk <= 0:
        p.errors.append("risk must be above $0")
    if p.r_multiple <= 0 and p.target_price is None:
        p.errors.append("target multiple must be above 0")
    for name, v in (("target", p.target_price), ("target 1", p.t1_price)):
        if v is not None and (v <= 0 or _to_tick(v, ROUND_HALF_UP) != v):
            p.errors.append(f"{name} must be a valid price in {fmt(tick(v if v > 0 else Decimal(1)))} steps")
            return p
    if p.stop <= 0 or _to_tick(p.stop, ROUND_HALF_UP) != p.stop:
        p.errors.append(f"stop must be a valid price in {fmt(tick(p.stop or Decimal(1)))} steps")
        return p

    long = side == "LONG"
    if limit_override is not None:
        lo = _price(limit_override)
        if lo is None or not lo.is_finite() or lo <= 0 or _to_tick(lo, ROUND_HALF_UP) != lo:
            p.errors.append("the locked entry price is not a valid price")
            return p
        p.limit = lo
    elif p.entry in ("cross", "market"):
        p.limit = (_to_tick(p.ask * (1 + ENTRY_OFFSET), ROUND_CEILING) if long
                   else _to_tick(p.bid * (1 - ENTRY_OFFSET), ROUND_FLOOR))
    elif p.entry == "mid":   # rounded toward a fill
        p.limit = _to_tick((p.bid + p.ask) / 2, ROUND_CEILING if long else ROUND_FLOOR)
    else:
        # Literal bid / ask, put on a valid tick BEFORE sizing (a sub-penny quote would otherwise
        # be sized at one price and sent at another): the bid rounds down, the ask up.
        p.limit = _to_tick(p.bid, ROUND_FLOOR) if p.entry == "bid" else _to_tick(p.ask, ROUND_CEILING)
    if long:
        if p.stop >= p.bid:
            p.errors.append(f"stop {fmt(p.stop)} must be below the bid {fmt(p.bid)}")
            return p
        p.per_share = p.limit - p.stop
    else:
        if p.stop <= p.ask:
            p.errors.append(f"stop {fmt(p.stop)} must be above the ask {fmt(p.ask)}")
            return p
        p.per_share = p.stop - p.limit

    if p.limit <= 0:
        p.errors.append("entry price rounds to zero")
        return p
    if p.per_share < MIN_STOP_TICKS * tick(p.limit):
        p.errors.append(f"stop is less than {MIN_STOP_TICKS} ticks from the entry")
        return p

    qty = int((p.risk / p.per_share).to_integral_value(rounding=ROUND_FLOOR))
    qty = min(qty, MAX_SHARES, int((MAX_NOTIONAL / p.limit).to_integral_value(rounding=ROUND_FLOOR)))
    p.qty = qty
    if qty < 1:
        p.errors.append(f"risk ${p.risk} is less than one share's risk (${p.per_share})")
        return p
    if p.target_price is not None:
        p.target = p.target_price
    else:
        move = p.per_share * p.r_multiple
        # Round the target toward the entry, so it is never further than R x risk.
        p.target = (_to_tick(p.limit + move, ROUND_FLOOR) if side == "LONG"
                    else _to_tick(p.limit - move, ROUND_CEILING))
    if p.target <= 0 or (p.target <= p.limit if side == "LONG" else p.target >= p.limit):
        p.errors.append(f"target {p.target} is not a valid price on the profitable side")
        p.qty = 0
        return p
    p.planned_risk = p.per_share * qty
    p.notional = p.limit * qty
    if p.split > 0:
        _two_targets(p)
    if p.ok and p.stop_type == "trail":
        _trailing_stop(p, trail_unit, trail_value, trail_basis)
    elif p.ok and p.stop_type == "autobe":
        p.be_trigger = p.limit + p.per_share if long else p.limit - p.per_share
    elif p.stop_type != "fixed":
        p.errors.append("stop type must be fixed, autobe or trail")
        p.qty = 0
    return p


def _trailing_stop(p: Plan, unit, value, basis) -> None:
    spec, err = trail_spec(unit, value, p.per_share, basis)
    if err:
        p.errors.append(err)
        p.qty = 0
        return
    p.trail = spec
    # Schwab starts the trail from its basis price after the fill: the bid for a long BID trail sits
    # below the entry, so take the less favourable of the entry and the basis price.
    ref = {"MARK": (p.bid + p.ask) / 2, "BID": p.bid, "ASK": p.ask, "LAST": p.limit}[spec["basis"]]
    ref = min(ref, p.limit) if p.side == "LONG" else max(ref, p.limit)
    p.trail_start = trail_level(ref, spec, p.side)
    wider = p.trail_start < p.stop if p.side == "LONG" else p.trail_start > p.stop
    if wider:
        extra = abs(p.trail_start - p.stop) * p.qty
        p.warnings.append(f"The trail starts at {fmt(p.trail_start)}, beyond your stop {fmt(p.stop)}: "
                          f"about ${extra:.2f} more risk than planned.")


def _two_targets(p: Plan) -> None:
    if not (0 < p.split < 1):
        p.errors.append("split must be between 0% and 100%")
    elif p.t1_price is None and p.t1_r <= 0:
        p.errors.append("target 1 multiple must be above 0")
    else:
        qty1 = int((p.qty * p.split).to_integral_value(rounding=ROUND_FLOOR))
        if qty1 < 1 or p.qty - qty1 < 1:
            p.errors.append(f"{p.qty} shares cannot be split {p.split * 100:g}% / {100 - p.split * 100:g}%")
        else:
            if p.t1_price is not None:
                t1 = p.t1_price
            else:
                move = p.per_share * p.t1_r
                t1 = (_to_tick(p.limit + move, ROUND_FLOOR) if p.side == "LONG"
                      else _to_tick(p.limit - move, ROUND_CEILING))
            beyond = t1 >= p.target if p.side == "LONG" else t1 <= p.target
            if t1 <= 0 or (t1 <= p.limit if p.side == "LONG" else t1 >= p.limit) or beyond:
                p.errors.append(f"target 1 {t1} is not a valid price between the entry and the runner")
            else:
                p.qty1, p.target1 = qty1, t1
                return
    p.qty = 0


def bracket_order(p: Plan, exit_duration: str = "DAY") -> dict:
    """Schwab 1st-triggers-OCO: entry LIMIT (or MARKET) -> OCO(target LIMIT, STOP market).

    Two targets (OT2OCO): entry LIMIT -> [OCO(target1, stop) for qty1, OCO(runner, stop) for
    the rest]. Each half has its own stop, so taking target 1 never cancels the runner's stop.
    """
    if not p.ok:
        raise ValueError("cannot build an order from an invalid plan: " + "; ".join(p.errors))
    entry_instr, exit_instr = ("BUY", "SELL") if p.side == "LONG" else ("SELL_SHORT", "BUY_TO_COVER")
    instrument = {"symbol": p.symbol, "assetType": "EQUITY"}

    def leg(instruction: str, qty: int) -> list[dict]:
        return [{"instruction": instruction, "quantity": qty, "instrument": instrument}]

    def oco(qty: int, target: Decimal) -> dict:
        return {
            "orderStrategyType": "OCO",
            "childOrderStrategies": [
                {"orderStrategyType": "SINGLE", "orderType": "LIMIT", "session": "NORMAL",
                 "duration": exit_duration, "price": fmt(target),
                 "orderLegCollection": leg(exit_instr, qty)},
                stop_leg(p.trail or p.stop, leg(exit_instr, qty), exit_duration),
            ],
        }

    exits = ([oco(p.qty1, p.target1), oco(p.qty - p.qty1, p.target)] if p.two_targets
             else [oco(p.qty, p.target)])
    order = {
        "orderStrategyType": "TRIGGER",
        "orderType": "MARKET" if p.market else "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderLegCollection": leg(entry_instr, p.qty),
        "childOrderStrategies": exits,
    }
    if not p.market:
        order["price"] = fmt(p.limit)
    return order
