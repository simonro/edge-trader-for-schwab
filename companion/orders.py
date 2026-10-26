"""Flatten Schwab order trees (SINGLE / OCO / TRIGGER / FLATTEN) into pending-order rows.

An OCO parent has no legs of its own, only childOrderStrategies; a TRIGGER parent
is a real order whose children wait (AWAITING_PARENT_ORDER) until it fills.
Multi-leg orders produce one row per leg; the order-level price is the net
strategy price and is kept on the order, never presented as a leg price.
Futures orders never appear in this endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass

TERMINAL = {"FILLED", "CANCELED", "REJECTED", "EXPIRED", "REPLACED"}
# Resting at the broker and able to execute now: the only states that protect a position.
LIVE = {"WORKING", "PENDING_ACTIVATION", "ACCEPTED", "QUEUED", "AWAITING_STOP_CONDITION",
        "AWAITING_CONDITION", "AWAITING_RELEASE_TIME", "NEW"}
STOP_TYPES = {"STOP", "STOP_LIMIT", "TRAILING_STOP", "TRAILING_STOP_LIMIT"}
SELLS = {"SELL", "SELL_TO_CLOSE", "SELL_SHORT", "SELL_TO_OPEN"}
BUYS = {"BUY", "BUY_TO_COVER", "BUY_TO_CLOSE", "BUY_TO_OPEN"}


@dataclass
class PendingOrder:
    order_id: int
    leg_id: int
    legs: int                # legs in this order (>1: a strategy)
    group_id: int            # top-level order id: rows sharing it are one OCO / bracket
    group_type: str          # SINGLE | OCO | TRIGGER | ...
    parent_id: int | None    # direct parent order (TRIGGER / OCO)
    oco_id: int | None       # the OCO container this order is an alternative in, at any depth
    status: str
    symbol: str
    underlying: str
    asset_type: str
    instruction: str         # BUY | SELL | SELL_SHORT | BUY_TO_COVER | BUY_TO_OPEN ...
    effect: str | None       # OPENING | CLOSING | None
    order_type: str          # LIMIT | STOP | STOP_LIMIT | TRAILING_STOP | MARKET | NET_DEBIT ...
    qty: float               # remaining quantity for this leg
    filled: float
    price: float | None      # limit price (net strategy price when legs > 1)
    stop: float | None       # stop trigger; None for trailing stops Schwab has not priced
    duration: str
    entered: str
    tag: str | None
    role: str                # STOP | TARGET | ENTRY | EXIT
    # Schwab-held trailing stop: distance and what it follows (None for every other order)
    trail_offset: float | None = None
    trail_link: str | None = None      # VALUE ($) | PERCENT | TICK
    trail_basis: str | None = None     # MARK | LAST | BID | ASK ...

    @property
    def live(self) -> bool:
        return self.status in LIVE

    def exits(self, side: str) -> bool:
        """Could this leg reduce a LONG/SHORT position (by instruction, not just symbol)?"""
        if self.effect == "OPENING":
            return False
        return self.instruction in (SELLS if side == "LONG" else BUYS) and \
            self.instruction not in ("SELL_SHORT", "SELL_TO_OPEN", "BUY_TO_OPEN")


def _num(v) -> float | None:
    return None if v is None else float(v)


def _role(order_type: str, effect: str | None, instruction: str) -> str:
    if effect == "OPENING" or instruction in ("SELL_SHORT", "BUY_TO_OPEN", "SELL_TO_OPEN"):
        return "ENTRY"
    if order_type in STOP_TYPES:
        return "STOP"
    if order_type == "LIMIT":
        return "TARGET"
    return "EXIT"


def _walk(o: dict, group_id: int, group_type: str, parent_id: int | None,
          out: list[PendingOrder], oco_id: int | None = None) -> None:
    status = o.get("status", "?")
    legs = o.get("orderLegCollection") or []
    if legs and status not in TERMINAL:
        order_qty = _num(o.get("quantity")) or 0.0
        remaining = _num(o.get("remainingQuantity"))
        remaining = order_qty if remaining is None else remaining
        order_type = o.get("orderType", "?")
        for i, leg in enumerate(legs):
            ins = leg.get("instrument") or {}
            ratio = (_num(leg.get("quantity")) or order_qty) / order_qty if order_qty else 1.0
            effect = leg.get("positionEffect")
            instruction = leg.get("instruction", "?")
            out.append(PendingOrder(
                order_id=o["orderId"], leg_id=leg.get("legId", i + 1), legs=len(legs),
                group_id=group_id, group_type=group_type, parent_id=parent_id, oco_id=oco_id,
                status=status,
                symbol=ins.get("symbol", "?"),
                underlying=ins.get("underlyingSymbol") or ins.get("symbol", "?"),
                asset_type=ins.get("assetType", "?"),
                instruction=instruction, effect=effect, order_type=order_type,
                qty=remaining * ratio,
                filled=_num(o.get("filledQuantity")) or 0.0,
                price=_num(o.get("price")),
                stop=_num(o.get("stopPrice")),
                duration=o.get("duration", "?"),
                entered=o.get("enteredTime", ""),
                tag=o.get("tag"),
                role=_role(order_type, effect, instruction),
                trail_offset=_num(o.get("stopPriceOffset")),
                trail_link=o.get("stopPriceLinkType"),
                trail_basis=o.get("stopPriceLinkBasis"),
            ))
    child_oco = o.get("orderId") if o.get("orderStrategyType") == "OCO" else oco_id
    for child in o.get("childOrderStrategies") or []:
        _walk(child, group_id, group_type, o.get("orderId"), out, child_oco)


def _statuses(o: dict, seen: dict[int, set[str]]) -> None:
    seen.setdefault(o.get("orderId"), set()).add(o.get("status", "?"))
    for child in o.get("childOrderStrategies") or []:
        _statuses(child, seen)


def terminal_ids(orders: list[dict]) -> set[int]:
    """Order ids that appear anywhere (top level or inside a tree) with a terminal status."""
    seen: dict[int, set[str]] = {}
    for o in orders:
        _statuses(o, seen)
    return {oid for oid, st in seen.items() if st & TERMINAL}


def pending_orders(orders: list[dict], known_terminal: set[int] | None = None) -> list[PendingOrder]:
    # When a bracket is attached in thinkorswim to an order that is already working, Schwab
    # keeps returning the original order on its own, frozen at WORKING, AND inside the new
    # OCO with its real status. Any terminal status for an order id wins, including one seen
    # on an earlier poll (known_terminal), since the 59-day WORKING sweep returns the frozen
    # copy without its finished OCO.
    done = terminal_ids(orders) | (known_terminal or set())
    out: list[PendingOrder] = []
    for o in orders:
        _walk(o, o["orderId"], o.get("orderStrategyType", "SINGLE"), None, out)
    out = [p for p in out if p.order_id not in done]
    keys: set[tuple[int, int]] = set()
    uniq = []
    for p in out:
        key = (p.order_id, p.leg_id)
        if key not in keys:
            keys.add(key)
            uniq.append(p)
    return sorted(uniq, key=lambda p: (p.underlying, p.group_id, p.role, p.order_id, p.leg_id))


def stop_coverage(pend: list[PendingOrder], symbol: str, side: str, qty: float):
    """Live stop protection for one position.

    OCO children are alternatives (one fills, the rest cancel), so each OCO group
    contributes at most its largest stop; independent groups add up; the total is
    capped at the position size. Returns (covered_qty, [(stop_price|None, qty)]).
    """
    # Alternatives inside one OCO container (top level or nested under a TRIGGER) count once;
    # every other live stop is independent.
    groups: dict[tuple, list[PendingOrder]] = {}
    for p in pend:
        if p.symbol == symbol and p.role == "STOP" and p.live and p.legs == 1 and p.exits(side):
            key = ("oco", p.oco_id) if p.oco_id is not None else ("single", p.order_id)
            groups.setdefault(key, []).append(p)
    alloc: list[tuple[float | None, float]] = []
    left = qty
    for group in groups.values():
        if left <= 1e-9:
            break
        best = max(group, key=lambda p: p.qty)
        take = min(best.qty, left)
        if take > 1e-9:
            alloc.append((best.stop, take))
            left -= take
    return qty - max(left, 0.0), alloc
