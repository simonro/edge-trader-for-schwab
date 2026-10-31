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
MAX_CONTRACTS = 100
MAX_NOTIONAL = Decimal("250000")
WIDE_SPREAD = Decimal("0.10")        # an option spread wider than 10% of its mid gets a warning
MIN_STOP_TICKS = 2


# Price grids ("ticks" profiles). Stocks: pennies from $1, 1/100 cent below. Options (exchange
# minimum increments): Penny Interval Program classes trade in $0.01 under $3 and $0.05 from $3,
# SPY / QQQ / IWM in $0.01 at every price, every other class in $0.05 under $3 and $0.10 from $3.
# When the penny flag is unknown an option gets the nickel grid: its prices are valid on every grid.
EQUITY = "EQUITY"
OPT_PENNY = "OPT_PENNY"
OPT_PENNY_ALL = "OPT_PENNY_ALL"
OPT_NICKEL = "OPT_NICKEL"
PENNY_ALL_ROOTS = frozenset({"SPY", "QQQ", "IWM"})
_C1, _C5, _C10 = Decimal("0.01"), Decimal("0.05"), Decimal("0.10")


def ticks_for(asset_type: str | None, underlying: str | None = None, is_penny: bool | None = None) -> str:
    """The price grid of an instrument (stocks and anything that is not an option: EQUITY)."""
    if (asset_type or "").upper() != "OPTION":
        return EQUITY
    if (underlying or "").upper().strip() in PENNY_ALL_ROOTS:
        return OPT_PENNY_ALL
    return OPT_PENNY if is_penny is True else OPT_NICKEL


def tick(price: Decimal, ticks: str = EQUITY) -> Decimal:
    if ticks == OPT_PENNY_ALL:
        return _C1
    if ticks == OPT_PENNY:
        return _C1 if price < 3 else _C5
    if ticks == OPT_NICKEL:
        return _C5 if price < 3 else _C10
    return Decimal("0.01") if price >= 1 else Decimal("0.0001")


def _to_tick(price: Decimal, rounding: str, ticks: str = EQUITY) -> Decimal:
    t = tick(price, ticks)
    out = (price / t).to_integral_value(rounding=rounding) * t
    # Rounding up across the $3 break can land between the two grids (2.98 -> 3.00 is fine, but a
    # coarser grid above $3 needs the result checked again on its own tick).
    t2 = tick(out, ticks)
    if t2 != t:
        out = (out / t2).to_integral_value(rounding=rounding) * t2
    return out


def fmt(price: Decimal, ticks: str = EQUITY) -> str:
    if ticks != EQUITY:
        return f"{price:.2f}"           # option prices are quoted in cents
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
    # Options: per_share stays the premium per unit; one contract is `multiplier` units, so the
    # dollars (risk, notional) are per_share x qty x multiplier. Stocks: multiplier 1.
    asset: str = "EQUITY"                 # EQUITY | OPTION
    multiplier: Decimal = Decimal(1)
    ticks: str = EQUITY
    size_mode: str = "risk"               # risk: qty from risk / stop distance; qty: the user's number
    has_target: bool = True               # False: no target is sent (add one later from the position)

    @property
    def has_stop(self) -> bool:
        return self.stop_type != "none"

    @property
    def option(self) -> bool:
        return self.asset == "OPTION"

    @property
    def unit_risk(self) -> Decimal:
        """Dollars at risk per share (stock) or per contract (option)."""
        return self.per_share * self.multiplier

    def f(self, price: Decimal) -> str:
        return fmt(price, self.ticks)

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
        f = self.f
        return {
            "symbol": self.symbol, "side": self.side, "stop": f(self.stop) if self.stop else None, "risk": float(self.risk),
            "r_multiple": float(self.r_multiple), "bid": float(self.bid), "ask": float(self.ask),
            "limit": f(self.limit) if self.limit else None, "qty": self.qty,
            "per_share": float(self.per_share), "target": f(self.target) if self.target else None,
            "planned_risk": float(self.planned_risk), "notional": float(self.notional),
            "split": float(self.split), "t1_r": float(self.t1_r), "qty1": self.qty1,
            "qty2": self.qty - self.qty1 if self.qty1 else 0,
            "target1": f(self.target1) if self.target1 else None,
            "entry": self.entry, "market": self.market, "stop_type": self.stop_type,
            "trail": ({"offset": float(self.trail["offset"]), "link": self.trail["link"], "basis": self.trail["basis"]}
                      if self.trail else None),
            "trail_start": f(self.trail_start) if self.trail_start else None, "warnings": self.warnings,
            "be_trigger": f(self.be_trigger) if self.be_trigger else None,
            "asset": self.asset, "multiplier": float(self.multiplier), "unit_risk": float(self.unit_risk),
            "size_mode": self.size_mode, "has_stop": self.has_stop, "has_target": self.has_target,
            "target_r": float(self._r_of(self.target)) if self.target and self.per_share else None,
            "target1_r": float(self._r_of(self.target1)) if self.target1 and self.per_share else None,
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


def trail_spec(unit: str, value, per_share: Decimal | None, basis: str = "MARK",
               ticks: str = EQUITY) -> tuple[dict | None, str | None]:
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
        off = _to_tick(per_share * v, ROUND_HALF_UP, ticks)
    elif unit == "USD":
        off = _to_tick(v, ROUND_HALF_UP, ticks)
    else:
        off = None
    if off is not None:
        if off <= 0:
            return None, f"trail distance rounds to 0 (prices move in {fmt(tick(v, ticks), ticks)} steps)"
        return {"offset": off, "link": "VALUE", "basis": basis}, None
    if unit == "PCT":
        if v >= 50:
            return None, "trail percent must be below 50"
        return {"offset": v.quantize(Decimal("0.01")), "link": "PERCENT", "basis": basis}, None
    return None, "trail unit must be R, $ or %"


def trail_level(from_price: Decimal, spec: dict, side: str, ticks: str = EQUITY) -> Decimal:
    """Where a trailing stop sits when the best price so far is from_price."""
    off = (from_price * spec["offset"] / 100) if spec["link"] == "PERCENT" else spec["offset"]
    raw = from_price - off if side == "LONG" else from_price + off
    return _to_tick(raw, ROUND_HALF_UP, ticks)


def stop_leg(spec, legs: list[dict], duration: str = "DAY", ticks: str = EQUITY) -> dict:
    """The stop child of an exit: a STOP at a price (Decimal) or a Schwab TRAILING_STOP (dict)."""
    if isinstance(spec, dict):
        off = spec["offset"]
        return {"orderStrategyType": "SINGLE", "orderType": "TRAILING_STOP", "session": "NORMAL",
                "duration": duration, "stopPriceLinkBasis": spec["basis"], "stopPriceLinkType": spec["link"],
                "stopPriceOffset": float(off), "orderLegCollection": legs}
    return {"orderStrategyType": "SINGLE", "orderType": "STOP", "session": "NORMAL", "duration": duration,
            "stopPrice": fmt(spec, ticks), "orderLegCollection": legs}


def plan(symbol: str, side: str, stop, risk, r_multiple, bid, ask, split=0, t1_r=1, entry="cross",
         target_price=None, t1_price=None, stop_type="fixed", trail_unit="R", trail_value=1,
         trail_basis="MARK", limit_override=None, asset="EQUITY", multiplier=1, ticks=None,
         size_mode="risk", qty=None, target=True, limit_price=None) -> Plan:
    """Size a bracket. Never moves the user's prices: an off-tick stop or target is an error, not rounded.

    split > 0 makes it a two-target entry: floor(qty x split) shares exit at target 1, the runner
    at the target, each half with its own stop (Schwab OT2OCO). Each target is at R x the risk
    per share, or at the price the user typed (target_price / t1_price), which wins over R.

    limit_override sizes from a given entry price instead of the quote (the click locks the price
    the ticket showed); the stop is still checked against the current bid / ask.

    asset="OPTION": a single call or put, LONG = buy to open, SHORT = sell to open. Stop and targets
    are option prices on the contract's grid (ticks); contracts are sized from risk / (risk per
    unit x multiplier). Options enter with a limit only (mid, bid or ask): no market or cross.

    size_mode="qty" sends the user's own quantity instead of sizing it from the risk. Only then may
    the ticket go without a stop (stop_type "none"); target=False sends no target. A missing exit can
    be added later from the position (Move stop / Replace target pair them into one OCO).

    entry="limit" sends the entry at the user's own limit_price (on the instrument's grid, never
    rounded); it does not follow the quote. A buy limit at or above the ask fills at once.
    """
    side = side.upper()
    stop_v = _price(stop)
    p = Plan(symbol.upper().strip(), side, stop_v if stop_v is not None else Decimal(0), Decimal(str(risk or 0)),
             Decimal(str(r_multiple or 0)), Decimal(str(bid or 0)), Decimal(str(ask or 0)),
             split=Decimal(str(split or 0)), t1_r=Decimal(str(t1_r or 0)), entry=(entry or "cross").lower(),
             target_price=_price(target_price), t1_price=_price(t1_price),
             stop_type=(stop_type or "fixed").lower())
    p.asset = (asset or "EQUITY").upper()
    p.multiplier = Decimal(str(multiplier or 1))
    p.ticks = ticks or ticks_for(p.asset)
    p.size_mode = (size_mode or "risk").lower()
    p.has_target = bool(target)
    tk, f = p.ticks, p.f
    if p.asset not in ("EQUITY", "OPTION"):
        p.errors.append(f"{p.asset} cannot be traded from the ticket")
        return p
    if p.option and p.multiplier != 100:
        p.errors.append(f"non-standard contract (multiplier {p.multiplier:g}): not traded from the ticket")
        return p
    if side not in ("LONG", "SHORT"):
        p.errors.append("side must be LONG or SHORT")
        return p
    if p.entry not in ("mid", "bid", "ask", "market", "cross", "limit"):
        p.errors.append("entry must be mid, bid, ask, limit, market or cross")
        return p
    if p.option and p.entry in ("market", "cross"):
        p.errors.append("options enter with a limit: pick Mid, Ask or Bid")
        return p
    if p.size_mode not in ("risk", "qty"):
        p.errors.append("size must be by risk or by quantity")
        return p
    if p.stop_type not in ("fixed", "autobe", "trail", "none"):
        p.errors.append("stop type must be fixed, autobe, trail or none")
        return p
    if p.stop_type == "none":
        p.stop = Decimal(0)                     # a typed stop is ignored when the ticket says no stop
        if p.size_mode == "risk":
            p.errors.append("sizing by risk needs a stop: type a stop, or size by quantity")
            return p
    # A stop price is needed to size by risk, for a fixed or Auto B/E stop, and for a trail in R.
    trail_r = p.stop_type == "trail" and (trail_unit or "R").upper() == "R"
    if p.stop_type != "none" and stop_v is None and (p.size_mode == "risk" or p.stop_type in ("fixed", "autobe") or trail_r):
        p.errors.append("type a stop price" + (" (or pick None)" if p.size_mode == "qty" else ""))
        return p
    typed = [v for v in (p.target_price, p.t1_price, stop_v) if v is not None]
    if any(not v.is_finite() for v in typed):
        p.errors.append("a typed price is not a number")
        return p
    if not all(v.is_finite() for v in (p.risk, p.r_multiple, p.bid, p.ask, p.split, p.t1_r)):
        p.errors.append("all numbers must be finite")
        return p
    if p.bid <= 0 or p.ask <= 0 or p.ask < p.bid:
        p.errors.append("no valid bid/ask quote")
        return p
    if p.size_mode == "risk" and p.risk <= 0:
        p.errors.append("risk must be above $0")
    if p.has_target and p.r_multiple <= 0 and p.target_price is None:
        p.errors.append("target multiple must be above 0")
    for name, v in (("target", p.target_price if p.has_target else None), ("target 1", p.t1_price)):
        if v is not None and (v <= 0 or _to_tick(v, ROUND_HALF_UP, tk) != v):
            p.errors.append(f"{name} must be a valid price in {f(tick(v if v > 0 else Decimal(1), tk))} steps")
            return p
    if p.stop and (p.stop <= 0 or _to_tick(p.stop, ROUND_HALF_UP, tk) != p.stop):
        p.errors.append(f"stop must be a valid price in {f(tick(p.stop if p.stop > 0 else Decimal(1), tk))} steps")
        return p
    if stop_v is not None and stop_v <= 0 and p.stop_type != "none":
        p.errors.append("stop must be above 0")
        return p

    long = side == "LONG"
    if limit_override is not None:
        lo = _price(limit_override)
        if lo is None or not lo.is_finite() or lo <= 0 or _to_tick(lo, ROUND_HALF_UP, tk) != lo:
            p.errors.append("the locked entry price is not a valid price")
            return p
        p.limit = lo
    elif p.entry == "limit":
        lp = _price(limit_price)
        if lp is None:
            p.errors.append("type the limit price")
            return p
        if not lp.is_finite() or lp <= 0 or _to_tick(lp, ROUND_HALF_UP, tk) != lp:
            p.errors.append(f"limit must be a valid price in {f(tick(lp if lp.is_finite() and lp > 0 else Decimal(1), tk))} steps")
            return p
        p.limit = lp
    elif p.entry in ("cross", "market"):
        p.limit = (_to_tick(p.ask * (1 + ENTRY_OFFSET), ROUND_CEILING, tk) if long
                   else _to_tick(p.bid * (1 - ENTRY_OFFSET), ROUND_FLOOR, tk))
    elif p.entry == "mid":   # rounded toward a fill
        p.limit = _to_tick((p.bid + p.ask) / 2, ROUND_CEILING if long else ROUND_FLOOR, tk)
    else:
        # Literal bid / ask, put on a valid tick BEFORE sizing (a sub-penny quote would otherwise
        # be sized at one price and sent at another): the bid rounds down, the ask up.
        p.limit = _to_tick(p.bid, ROUND_FLOOR, tk) if p.entry == "bid" else _to_tick(p.ask, ROUND_CEILING, tk)
    if p.stop:
        if long:
            if p.stop >= p.bid:
                p.errors.append(f"stop {f(p.stop)} must be below the bid {f(p.bid)}")
                return p
            p.per_share = p.limit - p.stop
        else:
            if p.stop <= p.ask:
                p.errors.append(f"stop {f(p.stop)} must be above the ask {f(p.ask)}")
                return p
            p.per_share = p.stop - p.limit

    if p.limit <= 0:
        p.errors.append("entry price rounds to zero")
        return p
    if p.stop and p.per_share <= 0:
        p.errors.append(f"stop {f(p.stop)} must be {'below' if long else 'above'} the entry {f(p.limit)}")
        return p
    if p.stop and p.per_share < MIN_STOP_TICKS * tick(p.limit, tk):
        p.errors.append(f"stop is less than {MIN_STOP_TICKS} ticks from the entry")
        return p

    cap = MAX_CONTRACTS if p.option else MAX_SHARES
    by_notional = int((MAX_NOTIONAL / (p.limit * p.multiplier)).to_integral_value(rounding=ROUND_FLOOR))
    unit_word = "contracts" if p.option else "shares"
    if p.size_mode == "qty":
        try:
            n = Decimal(str(qty))
        except Exception:
            n = Decimal("NaN")
        if not n.is_finite() or n != n.to_integral_value() or n < 1:
            p.errors.append(f"type how many {unit_word} (a whole number, at least 1)")
            return p
        n = int(n)
        if n > cap:
            p.errors.append(f"at most {cap} {unit_word} per ticket")
            return p
        if n > by_notional:
            p.errors.append(f"{n} {unit_word} is over the ${MAX_NOTIONAL:,.0f} notional cap (at most {by_notional})")
            return p
        p.qty = n
    else:
        n = int((p.risk / p.unit_risk).to_integral_value(rounding=ROUND_FLOOR))
        p.qty = min(n, cap, by_notional)
        if p.qty < 1:
            one = "contract" if p.option else "share"
            p.errors.append(f"risk ${p.risk} is less than one {one}'s risk (${p.unit_risk:.2f})")
            return p
    qty = p.qty
    if p.has_target:
        if p.target_price is not None:
            p.target = p.target_price
        elif not p.per_share:
            p.errors.append("with no stop there is no R: type the target price")
            p.qty = 0
            return p
        else:
            move = p.per_share * p.r_multiple
            # Round the target toward the entry, so it is never further than R x risk.
            p.target = (_to_tick(p.limit + move, ROUND_FLOOR, tk) if side == "LONG"
                        else _to_tick(p.limit - move, ROUND_CEILING, tk))
        if p.target <= 0 and p.target_price is None:
            p.errors.append(f"a {p.r_multiple:g}R target would be at or below $0, not a valid price on the profitable side: "
                            "pick a smaller R or type a target price")
            p.qty = 0
            return p
        if p.target <= 0 or (p.target <= p.limit if side == "LONG" else p.target >= p.limit):
            p.errors.append(f"target {f(p.target) if p.target > 0 else p.target} is not a valid price on the profitable side")
            p.qty = 0
            return p
    p.planned_risk = p.per_share * qty * p.multiplier
    p.notional = p.limit * qty * p.multiplier
    if p.entry == "limit" and (p.limit >= p.ask if long else p.limit <= p.bid):
        p.warnings.append(f"Your limit {f(p.limit)} is through the {'ask' if long else 'bid'} "
                          f"{f(p.ask if long else p.bid)}: it fills at once, at that price or better.")
    if p.option:
        _option_warnings(p)
    if p.split > 0:
        if not p.has_target or p.stop_type == "none":
            p.errors.append("two targets need a stop and a target")
            p.qty = 0
            return p
        _two_targets(p)
    if p.ok and p.stop_type == "trail":
        _trailing_stop(p, trail_unit, trail_value, trail_basis)
    elif p.ok and p.stop_type == "autobe":
        p.be_trigger = p.limit + p.per_share if long else p.limit - p.per_share
    if p.ok and p.stop_type == "none":
        p.warnings.insert(0, "NO STOP: nothing protects this position at Schwab until you add one in Manage.")
        if p.option and p.side == "SHORT":
            p.warnings.insert(1, "A short option with no stop has no limit on the loss.")
    return p


def _trailing_stop(p: Plan, unit, value, basis) -> None:
    spec, err = trail_spec(unit, value, p.per_share or None, basis, p.ticks)
    if err:
        p.errors.append(err)
        p.qty = 0
        return
    p.trail = spec
    # Schwab starts the trail from its basis price after the fill: the bid for a long BID trail sits
    # below the entry, so take the less favourable of the entry and the basis price.
    ref = {"MARK": (p.bid + p.ask) / 2, "BID": p.bid, "ASK": p.ask, "LAST": p.limit}[spec["basis"]]
    ref = min(ref, p.limit) if p.side == "LONG" else max(ref, p.limit)
    p.trail_start = trail_level(ref, spec, p.side, p.ticks)
    wider = bool(p.stop) and (p.trail_start < p.stop if p.side == "LONG" else p.trail_start > p.stop)
    if wider:
        extra = abs(p.trail_start - p.stop) * p.qty * p.multiplier
        p.warnings.append(f"The trail starts at {p.f(p.trail_start)}, beyond your stop {p.f(p.stop)}: "
                          f"about ${extra:.2f} more risk than planned.")


def _two_targets(p: Plan) -> None:
    if not (0 < p.split < 1):
        p.errors.append("split must be between 0% and 100%")
    elif p.t1_price is None and p.t1_r <= 0:
        p.errors.append("target 1 multiple must be above 0")
    else:
        qty1 = int((p.qty * p.split).to_integral_value(rounding=ROUND_FLOOR))
        if qty1 < 1 or p.qty - qty1 < 1:
            p.errors.append(f"{p.qty} {'contracts' if p.option else 'shares'} cannot be split "
                            f"{p.split * 100:g}% / {100 - p.split * 100:g}%")
        else:
            if p.t1_price is not None:
                t1 = p.t1_price
            else:
                move = p.per_share * p.t1_r
                t1 = (_to_tick(p.limit + move, ROUND_FLOOR, p.ticks) if p.side == "LONG"
                      else _to_tick(p.limit - move, ROUND_CEILING, p.ticks))
            beyond = t1 >= p.target if p.side == "LONG" else t1 <= p.target
            if t1 <= 0 or (t1 <= p.limit if p.side == "LONG" else t1 >= p.limit) or beyond:
                p.errors.append(f"target 1 {t1} is not a valid price between the entry and the runner")
            else:
                p.qty1, p.target1 = qty1, t1
                return
    p.qty = 0


def put_call(symbol: str) -> str | None:
    """"C" or "P" from an OCC option symbol (root padded to 6, YYMMDD, C/P, strike x 1000)."""
    s = symbol.strip()
    return s[-9] if len(s) >= 15 and s[-9] in "CP" else None


def _option_warnings(p: Plan) -> None:
    mid = (p.bid + p.ask) / 2
    if mid > 0 and (p.ask - p.bid) / mid > WIDE_SPREAD:
        p.warnings.append(f"Wide market: {p.f(p.bid)} / {p.f(p.ask)} is {(p.ask - p.bid) / mid * 100:.0f}% of the mid. "
                          "A stop can trigger on a quote that briefly widens.")
    if p.side == "SHORT":
        p.warnings.append("Sell to open: a short option can lose far more than planned if price gaps past the stop. "
                          "Schwab refuses it if your account lacks the option approval level.")
        if put_call(p.symbol) == "C":
            p.warnings.append("A short call can be assigned early, most often before an ex-dividend date.")


def bracket_order(p: Plan, exit_duration: str = "DAY") -> dict:
    """Schwab 1st-triggers-OCO: entry LIMIT (or MARKET) -> OCO(target LIMIT, STOP market).

    Two targets (OT2OCO): entry LIMIT -> [OCO(target1, stop) for qty1, OCO(runner, stop) for
    the rest]. Each half has its own stop, so taking target 1 never cancels the runner's stop.

    Without a target the entry triggers the stop alone; without a stop, the target alone; with
    neither, the entry is a plain SINGLE order.
    """
    if not p.ok:
        raise ValueError("cannot build an order from an invalid plan: " + "; ".join(p.errors))
    entry_instr, exit_instr = {("EQUITY", "LONG"): ("BUY", "SELL"), ("EQUITY", "SHORT"): ("SELL_SHORT", "BUY_TO_COVER"),
                               ("OPTION", "LONG"): ("BUY_TO_OPEN", "SELL_TO_CLOSE"),
                               ("OPTION", "SHORT"): ("SELL_TO_OPEN", "BUY_TO_CLOSE")}[(p.asset, p.side)]
    instrument = {"symbol": p.symbol, "assetType": p.asset}

    def leg(instruction: str, qty: int) -> list[dict]:
        return [{"instruction": instruction, "quantity": qty, "instrument": instrument}]

    def oco(qty: int, target: Decimal) -> dict:
        return {
            "orderStrategyType": "OCO",
            "childOrderStrategies": [
                {"orderStrategyType": "SINGLE", "orderType": "LIMIT", "session": "NORMAL",
                 "duration": exit_duration, "price": p.f(target),
                 "orderLegCollection": leg(exit_instr, qty)},
                stop_leg(p.trail or p.stop, leg(exit_instr, qty), exit_duration, p.ticks),
            ],
        }

    if p.two_targets:
        exits = [oco(p.qty1, p.target1), oco(p.qty - p.qty1, p.target)]
    elif p.has_stop and p.has_target:
        exits = [oco(p.qty, p.target)]
    elif p.has_stop:
        exits = [stop_leg(p.trail or p.stop, leg(exit_instr, p.qty), exit_duration, p.ticks)]
    elif p.has_target:
        exits = [{"orderStrategyType": "SINGLE", "orderType": "LIMIT", "session": "NORMAL",
                  "duration": exit_duration, "price": p.f(p.target), "orderLegCollection": leg(exit_instr, p.qty)}]
    else:
        exits = []
    order = {
        "orderStrategyType": "TRIGGER" if exits else "SINGLE",
        "orderType": "MARKET" if p.market else "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderLegCollection": leg(entry_instr, p.qty),
    }
    if exits:
        order["childOrderStrategies"] = exits
    if not p.market:
        order["price"] = p.f(p.limit)
    return order
