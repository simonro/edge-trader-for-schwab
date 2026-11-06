"""Option spreads: the ledger the companion's own spread tickets own.

the spreads plan, slice 1. A spread is created only by a companion ticket (slice 2); a
spread entered in thinkorswim stays separate legs. Each contract of an active spread is
reserved: no other spread may hold it, and the single-leg actions (Close, partial, stop,
target, trail, cancel, a new ticket) refuse it, so a leg can never be dismantled alone.

Fills are persisted per order and leg as soon as an order shows them, so a filled entry from an
earlier day (no longer in today's order read) still counts after a restart. `reconcile` turns
the ledger, the orders seen and the account's quantities into one state per spread:

    PENDING        entry working, nothing filled
    PARTIAL        entry working with fills (UNBALANCED in the detail when the legs differ)
    OPEN           entry final, balanced fills, the account holds exactly that (a GTC profit
                   target may be working: its detail says so, and a target fill closes it)
    CLOSING        a close order is working (or partly filled)
    CLOSED         closed by its own close order and the account is flat on every leg   (final)
    CANCELED       entry final with nothing filled                                       (final)
    BROKEN         the account disagrees with the ledger twice in a row (closed or added
                   elsewhere, a leg missing, possible assignment)
    EXPIRED_CHECK  past expiration: settlement or assignment must be checked before final P&L
    NEEDS_ACTION   the ledger itself cannot be balanced (unbalanced entry left after it ended)
    UNKNOWN        not enough fresh evidence yet
    SENDING        the entry is being sent (or the app stopped while sending it)

Only CLOSED and CANCELED release the reservations; every other state keeps both contracts
blocked until the user presses Resolve on the spread (RESOLVED, final).

The spread's exits are its close order (DAY, one click) and its target (GTC, slice 3); both count as
close fills. At most one of them works at a time.

A close whose outcome is unclear (no answer, 5xx, accepted without an order id) leaves a HOLD:
the spread shows NEEDS_ACTION whatever the evidence says, so no second close is ever sent for
it. Only Resolve (after checking thinkorswim) clears a hold.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime, timezone

from ..orders import TERMINAL
from ..store import Store, now_iso
from .spread_stop import StopWatch

SCHEMA = """
create table if not exists spreads (
    spread_id text primary key, ticket_id text, underlying text, kind text, qty integer,
    expiration text, state text, detail text, entry_order_id integer, close_order_id integer,
    created text, updated text);
create table if not exists spread_legs (
    spread_id text, leg integer, symbol text, side text, ratio integer, primary key (spread_id, leg));
create table if not exists spread_reservations (symbol text primary key, spread_id text, created text);
create table if not exists spread_fills (
    spread_id text, order_id integer, role text, symbol text, filled real, status text, updated text,
    primary key (spread_id, order_id, symbol));
create table if not exists spread_events (
    spread_id text, created text, state text, order_id integer, detail text);
create table if not exists spread_tickets (
    spread_id text primary key, ticket_id text unique, put_call text, width real, entry_limit real,
    max_loss real, max_gain real, breakeven real, request text, preview text, created text);
create table if not exists spread_holds (spread_id text primary key, reason text, created text);
"""

FINAL = {"CLOSED", "CANCELED", "RESOLVED"}
RELEASE = FINAL
NOTHING_FILLED = {"CANCELED", "REJECTED", "EXPIRED"}         # final statuses that can end with no fill
RESOLVABLE = {"BROKEN", "EXPIRED_CHECK", "NEEDS_ACTION", "UNKNOWN", "SENDING"}
OCC_DATE = re.compile(r"^[A-Z0-9.]{1,6} *(\d{6})[CP]\d{8}$")
ROLES = ("entry", "close", "target")        # a spread's own orders
EXITS = ("close", "target")


def own_orders(sp: dict) -> list[tuple[str, int]]:
    """(role, order id) of the spread's own orders that exist."""
    return [(r, sp.get(f"{r}_order_id")) for r in ROLES if sp.get(f"{r}_order_id") is not None]


def expiration_of(symbol: str) -> date | None:
    m = OCC_DATE.match((symbol or "").upper())
    if not m:
        return None
    d = m.group(1)
    return date(2000 + int(d[:2]), int(d[2:4]), int(d[4:6]))


def _find(orders: list[dict], oid: int | None) -> dict | None:
    def walk(o):
        if o.get("orderId") == oid:
            return o
        for ch in o.get("childOrderStrategies") or []:
            hit = walk(ch)
            if hit:
                return hit
    if oid is None:
        return None
    for o in orders:
        hit = walk(o)
        if hit:
            return hit
    return None


def leg_fills(order: dict) -> dict[str, float]:
    """Filled quantity per contract of one (multi-leg) order, from its execution legs."""
    by_leg = {leg.get("legId"): (leg.get("instrument") or {}).get("symbol")
              for leg in order.get("orderLegCollection") or []}
    out = {s: 0.0 for s in by_leg.values() if s}
    for act in order.get("orderActivityCollection") or []:
        if act.get("executionType") != "FILL":
            continue
        for ex in act.get("executionLegs") or []:
            sym = by_leg.get(ex.get("legId"))
            if sym:
                out[sym] = out.get(sym, 0.0) + float(ex.get("quantity") or 0)
    return out


class SpreadBook:
    """The spread ledger on the companion's SQLite store."""

    def __init__(self, store: Store):
        self.store = store
        with store._lock, store._con:
            store._con.executescript(SCHEMA)
            cols = {r[1] for r in store._con.execute("pragma table_info(spreads)")}
            if "target_order_id" not in cols:
                store._con.execute("alter table spreads add column target_order_id integer")
        self._mismatch: dict[str, tuple[int, object]] = {}   # per spread: (disagreements, last observation)
        self.stops = StopWatch(store)                          # app-watched stops (slice 4)
        self.on_fire = None          # set by the routes: send a fired stop's close (Send when hit)

    # ---- ledger -------------------------------------------------------------------------

    def create(self, ticket_id: str, underlying: str, kind: str, legs: list[dict], qty: int,
               state: str = "PENDING", ticket: dict | None = None) -> str | None:
        """A new spread and its reservations in one transaction; None when any contract is
        already held by another active spread (no shared contracts in phase 1). `ticket` holds
        what the ticket showed (width, entry limit, max loss and gain, breakeven, the request)."""
        sid = "sp-" + uuid.uuid4().hex[:12]
        exp = min((expiration_of(l["symbol"]) for l in legs), default=None)
        con = self.store._con
        with self.store._lock:
            try:
                with con:
                    now = now_iso()
                    con.execute("insert into spreads (spread_id, ticket_id, underlying, kind, qty, expiration, "
                                "state, detail, entry_order_id, close_order_id, created, updated) "
                                "values (?,?,?,?,?,?,?,?,?,?,?,?)",
                                (sid, ticket_id, underlying, kind, qty, exp.isoformat() if exp else None,
                                 state, "created", None, None, now, now))
                    for i, l in enumerate(legs):
                        con.execute("insert into spread_legs values (?,?,?,?,?)",
                                    (sid, i, l["symbol"], l["side"], int(l.get("ratio", 1))))
                        con.execute("insert into spread_reservations values (?,?,?)", (l["symbol"], sid, now))
                    con.execute("insert into spread_events values (?,?,?,?,?)", (sid, now, state, None, "created"))
                    if ticket is not None:
                        con.execute("insert into spread_tickets values (?,?,?,?,?,?,?,?,?,?,?)",
                                    (sid, ticket_id, ticket.get("put_call"), ticket.get("width"),
                                     ticket.get("entry_limit"), ticket.get("max_loss"), ticket.get("max_gain"),
                                     ticket.get("breakeven"), json.dumps(ticket.get("request")),
                                     json.dumps(ticket.get("preview")), now))
            except Exception as e:   # sqlite3.IntegrityError: a contract is already reserved
                if "UNIQUE" in str(e) or "constraint" in str(e).lower():
                    return None
                raise
        return sid

    def attach_order(self, spread_id: str, role: str, order_id: int) -> None:
        col = {"entry": "entry_order_id", "close": "close_order_id", "target": "target_order_id"}[role]
        with self.store._lock, self.store._con:
            self.store._con.execute(f"update spreads set {col} = ?, updated = ? where spread_id = ?",
                                    (order_id, now_iso(), spread_id))
            self.store._con.execute("insert into spread_events values (?,?,?,?,?)",
                                    (spread_id, now_iso(), f"{role.upper()}_ORDER", order_id, ""))

    def get(self, spread_id: str) -> dict | None:
        with self.store._lock:
            cur = self.store._con.execute("select * from spreads where spread_id = ?", (spread_id,))
            row = cur.fetchone()
            if not row:
                return None
            sp = dict(zip([c[0] for c in cur.description], row))
            sp["legs"] = [dict(zip(("leg", "symbol", "side", "ratio"), r)) for r in self.store._con.execute(
                "select leg, symbol, side, ratio from spread_legs where spread_id = ? order by leg", (spread_id,))]
            sp["fills"] = [dict(zip(("order_id", "role", "symbol", "filled", "status"), r)) for r in
                           self.store._con.execute("select order_id, role, symbol, filled, status from spread_fills "
                                                   "where spread_id = ?", (spread_id,))]
            t = self.store._con.execute("select put_call, width, entry_limit, max_loss, max_gain, breakeven "
                                        "from spread_tickets where spread_id = ?", (spread_id,)).fetchone()
            sp["ticket"] = dict(zip(("put_call", "width", "entry_limit", "max_loss", "max_gain", "breakeven"), t))                 if t else None
            h = self.store._con.execute("select reason, created from spread_holds where spread_id = ?",
                                        (spread_id,)).fetchone()
            sp["hold"] = {"reason": h[0], "created": h[1]} if h else None
        return sp

    @staticmethod
    def held_units(sp: dict) -> float:
        """Spreads held by the ledger: entry fills minus close fills, per leg over its ratio, the
        smallest leg (a balanced spread has the same on both)."""
        per = []
        for l in sp["legs"]:
            got = sum(f["filled"] if f["role"] == "entry" else -f["filled"]
                      for f in sp["fills"] if f["symbol"] == l["symbol"])
            per.append(got / abs(l["ratio"] or 1))
        return min(per) if per else 0.0

    def by_ticket(self, ticket_id: str) -> dict | None:
        with self.store._lock:
            row = self.store._con.execute("select spread_id from spreads where ticket_id = ?", (ticket_id,)).fetchone()
        return self.get(row[0]) if row else None

    def unconfirmed(self) -> dict | None:
        """An active spread whose entry has no order id (sending, or sent with no clear answer):
        no new spread ticket goes out until it is resolved."""
        return next((sp for sp in self.active() if sp["entry_order_id"] is None), None)

    def hold(self, spread_id: str, reason: str) -> None:
        """An unclear close: the spread stays NEEDS_ACTION until Resolve (never closed twice)."""
        with self.store._lock, self.store._con:
            self.store._con.execute("insert into spread_holds values (?,?,?) on conflict (spread_id) do update "
                                    "set reason = excluded.reason", (spread_id, reason[:500], now_iso()))
        self.set_state(spread_id, "NEEDS_ACTION", reason)

    def clear_hold(self, spread_id: str) -> None:
        with self.store._lock, self.store._con:
            self.store._con.execute("delete from spread_holds where spread_id = ?", (spread_id,))

    @staticmethod
    def view(sp: dict) -> dict:
        """What the dashboard shows of a spread."""
        return {k: sp.get(k) for k in ("spread_id", "underlying", "kind", "qty", "state", "detail", "legs",
                                       "expiration", "entry_order_id", "close_order_id", "target_order_id",
                                       "created", "ticket", "hold")}

    def watch_stops(self, views: list[dict], quotes: dict, market_open: bool) -> list[dict]:
        """Each shown spread with its watch-only stop status (`stop`, or None), after one look at the
        poller's leg quotes. Sends nothing."""
        for v in views:
            try:
                sp = self.get(v["spread_id"])
                v["stop"] = self.stops.watch(sp, quotes.get, market_open) if sp else None
                if v["stop"] and v["stop"].pop("fired_now", False) and v["stop"].get("send"):
                    try:
                        if not self.on_fire:
                            raise RuntimeError("no sender in this build")
                        self.on_fire(v["spread_id"])         # the desk sends ONE close, off this thread
                    except Exception as e:
                        self.stops.mark_unsent(v["spread_id"], f"the send could not start ({type(e).__name__})")
            except Exception as e:      # never break the dashboard's rebuild: show the stop as not watched
                self.stops._obs.pop(v["spread_id"], None)       # and a half-confirmed hit does not carry over
                s = self.stops.get(v["spread_id"])
                v["stop"] = s and {**s, "watch_only": True, "mid": None, "status": "suspended",
                                   "detail": f"watch error: {type(e).__name__}"}
        return views

    def active(self) -> list[dict]:
        with self.store._lock:
            ids = [r[0] for r in self.store._con.execute(
                "select spread_id from spreads where state not in (?,?,?) order by created", tuple(FINAL))]
        return [self.get(i) for i in ids]

    def closed_since(self, since_iso: str) -> list[dict]:
        """Spreads closed by their own close order since this time (UTC ISO): Closed today shows
        each as one round trip. Spreads closed elsewhere (resolved) stay as their legs."""
        with self.store._lock:
            ids = [r[0] for r in self.store._con.execute(
                "select spread_id from spreads where state = 'CLOSED' and updated >= ? order by updated", (since_iso,))]
        return [self.get(i) for i in ids]

    def owner_of(self, symbols) -> dict | None:
        """The active spread holding any of these contracts, or None."""
        syms = [s for s in symbols if s]
        if not syms:
            return None
        with self.store._lock:
            row = self.store._con.execute(
                f"select spread_id from spread_reservations where symbol in ({','.join('?' * len(syms))}) limit 1",
                syms).fetchone()
        return self.get(row[0]) if row else None

    def events(self, spread_id: str) -> list[dict]:
        with self.store._lock:
            return [dict(zip(("created", "state", "order_id", "detail"), r)) for r in self.store._con.execute(
                "select created, state, order_id, detail from spread_events where spread_id = ? order by rowid",
                (spread_id,))]

    def set_state(self, spread_id: str, state: str, detail: str = "", order_id: int | None = None) -> bool:
        """Record a state change (an event only when it changes) and release the reservations on
        a final state. Returns whether anything changed."""
        with self.store._lock, self.store._con:
            con = self.store._con
            row = con.execute("select state, detail from spreads where spread_id = ?", (spread_id,)).fetchone()
            if not row or (row[0], row[1]) == (state, detail):
                return False
            if row[0] in FINAL:
                return False              # a final spread never comes back
            now = now_iso()
            con.execute("update spreads set state = ?, detail = ?, updated = ? where spread_id = ?",
                        (state, detail[:500], now, spread_id))
            con.execute("insert into spread_events values (?,?,?,?,?)", (spread_id, now, state, order_id, detail[:500]))
            if state in RELEASE:
                con.execute("delete from spread_reservations where spread_id = ?", (spread_id,))
        return True

    def live_order_ids(self, sp: dict) -> list[int]:
        """The spread's own orders last seen in a non-final status (they can still execute)."""
        last = {f["order_id"]: f["status"] for f in sp["fills"]}
        return [oid for _, oid in own_orders(sp) if last.get(oid) is not None and last[oid] not in TERMINAL]

    def resolve(self, spread_id: str, note: str = "resolved by the user") -> tuple[bool, str | None]:
        """Release a spread the user checked in thinkorswim. Only a spread the evidence cannot settle
        (BROKEN, EXPIRED_CHECK, NEEDS_ACTION, UNKNOWN) and none of whose own orders is still live."""
        sp = self.get(spread_id)
        if not sp:
            return False, "No such spread."
        if sp["state"] not in RESOLVABLE:
            return False, (f"The spread is {sp['state']}: nothing to resolve." if sp["state"] in FINAL else
                           f"The spread is {sp['state']}; Resolve is only for BROKEN, EXPIRED_CHECK, "
                           "NEEDS_ACTION or UNKNOWN.")
        live = self.live_order_ids(sp)
        if live:
            return False, (f"Order(s) {', '.join(map(str, live))} of this spread may still execute: cancel them "
                           "in thinkorswim first, then Resolve.")
        changed = self.set_state(spread_id, "RESOLVED", note)
        if changed:
            self.clear_hold(spread_id)
        return changed, None

    def _save_fills(self, spread_id: str, role: str, order: dict) -> None:
        now = now_iso()
        with self.store._lock, self.store._con:
            for sym, q in leg_fills(order).items():
                self.store._con.execute(
                    "insert into spread_fills values (?,?,?,?,?,?,?) on conflict (spread_id, order_id, symbol) do "
                    "update set filled = max(spread_fills.filled, excluded.filled), status = excluded.status, "
                    "updated = excluded.updated",
                    (spread_id, order.get("orderId"), role, sym, q, order.get("status"), now))

    # ---- reconcile ----------------------------------------------------------------------

    def to_follow(self, orders: list[dict]) -> list[int]:
        """Own orders the poller should ask Schwab about: not in the orders it holds and not
        known final here (an older GTC that finished, or one that finished while the app was off)."""
        out = []
        for sp in self.active():
            last = {f["order_id"]: f["status"] for f in sp["fills"]}
            for _, oid in own_orders(sp):
                if _find(orders, oid):
                    continue
                if last.get(oid) not in TERMINAL or not self._complete(sp, oid):
                    out.append(oid)
        return out

    def _complete(self, sp: dict, oid: int) -> bool:
        """Whether the saved order's filledQuantity is fully backed by its execution legs."""
        o = self.store.get_order(oid)
        if not o:
            return False
        ratio = {l["symbol"]: abs(l["ratio"]) for l in sp["legs"]}
        got = [f["filled"] / ratio.get(f["symbol"], 1) for f in sp["fills"] if f["order_id"] == oid]
        return float(o.get("filledQuantity") or 0) <= (min(got) if got else 0.0) + 1e-9

    def reconcile(self, orders: list[dict], positions: dict[str, float] | None, today: date,
                  evidence_fresh: bool, observation=None) -> list[dict]:
        """Update every active spread from the orders seen (or else their last saved version) and
        the account's quantities (symbol -> signed quantity). With stale evidence nothing moves to
        BROKEN, CLOSED or CANCELED. `observation` names the account read (its time): a
        disagreement counts toward BROKEN once per distinct read. Returns the active spreads."""
        out = []
        for sp in self.active():
            own = []
            for role, oid in own_orders(sp):
                o = _find(orders, oid) or self.store.get_order(oid)
                if o:
                    own.append(o)
                    self._save_fills(sp["spread_id"], role, o)
            sp = self.get(sp["spread_id"])
            state, detail = self.judge(sp, own, positions, today, evidence_fresh)
            just_sent = sp["state"] in ("PENDING", "CLOSING") or (sp["state"] == "OPEN" and detail.startswith("target"))
            if state == "UNKNOWN" and detail.endswith("not seen yet") and just_sent \
                    and _age_seconds(sp.get("updated")) < JUST_SENT:
                # Schwab lists a new order a few seconds after accepting it: not a reason to alarm.
                state, detail = sp["state"], sp.get("detail") or ""
            if sp.get("hold"):
                # An unclear close outranks the evidence: never let it look OPEN (closable) again.
                state, detail = "NEEDS_ACTION", f"{sp['hold']['reason']} (now: {detail or sp['state']})"
            if state == "BROKEN":
                n, seen = self._mismatch.get(sp["spread_id"], (0, object()))
                if _new_observation(observation, seen):
                    n, seen = n + 1, observation          # remember the last COUNTED observation
                self._mismatch[sp["spread_id"]] = (n, seen)
                if n < 2:
                    state, detail = sp["state"], f"checking: {detail}"
            else:
                self._mismatch.pop(sp["spread_id"], None)
            if state is not None:
                self.set_state(sp["spread_id"], state, detail)
            out.append(self.get(sp["spread_id"]))
        return out

    @staticmethod
    def judge(sp: dict, orders: list[dict], positions: dict[str, float] | None, today: date,
              evidence_fresh: bool) -> tuple[str | None, str]:
        """The state the evidence supports (None: keep the current one)."""
        legs = sp["legs"]
        syms = [l["symbol"] for l in legs]
        sign = {l["symbol"]: (1 if l["side"] == "LONG" else -1) * l["ratio"] for l in legs}

        def filled(*roles):
            per = {s: 0.0 for s in syms}
            for f in sp["fills"]:
                if f["role"] in roles and f["symbol"] in per:
                    per[f["symbol"]] += f["filled"]
            return per

        def units(per):        # spreads filled per leg (fills divided by the leg ratio)
            return {s: per[s] / abs(sign[s]) for s in syms}

        fe, fc = units(filled("entry")), units(filled(*EXITS))

        def seen(oid):
            o = _find(orders, oid) if oid is not None else None
            return o, (o.get("status") if o else next((f["status"] for f in sp["fills"] if f["order_id"] == oid), None))

        entry, e_status = seen(sp["entry_order_id"])
        close, c_status = seen(sp["close_order_id"])
        target, t_status = seen(sp.get("target_order_id"))
        balanced = len({round(v, 6) for v in fe.values()}) == 1
        rem = {s: fe[s] - fc[s] for s in syms}
        held_units = min(rem.values()) if rem else 0.0
        expected = {s: sign[s] * (fe[s] - fc[s]) for s in syms}
        exp = date.fromisoformat(sp["expiration"]) if sp.get("expiration") else None

        if sp["entry_order_id"] is None:
            return None, sp.get("detail") or ""
        if e_status is None:
            return ("UNKNOWN", "entry order not seen yet") if evidence_fresh else (None, "")
        if "REPLACED" in (e_status, c_status, t_status):
            return "NEEDS_ACTION", ("the spread's order was replaced outside the companion (changed in "
                                    "thinkorswim?): check it, then Resolve")
        if not balanced:
            txt = "UNBALANCED fills: " + ", ".join(f"{s.strip()} {fe[s]:g}" for s in syms)
            return ("PARTIAL" if e_status not in TERMINAL else "NEEDS_ACTION"), txt
        if entry and float(entry.get("filledQuantity") or 0) > min(fe.values()) + 1e-9:
            return "UNKNOWN", "Schwab reports more filled than its execution details show yet"
        for role, oid, st in (("close", sp["close_order_id"], c_status), ("target", sp.get("target_order_id"), t_status)):
            if oid is not None and st is None:
                return ("UNKNOWN", f"{role} order not seen yet") if evidence_fresh else (None, "")
        exit_working = any(st is not None and st not in TERMINAL for st in (c_status, t_status))
        if len({round(v, 6) for v in rem.values()}) > 1:
            txt = "UNBALANCED close fills: " + ", ".join(f"{s.strip()} {rem[s]:g} left" for s in syms)
            return ("CLOSING" if exit_working else "NEEDS_ACTION"), txt
        for o, oid in ((close, sp["close_order_id"]), (target, sp.get("target_order_id"))):
            this = units({s: sum(f["filled"] for f in sp["fills"] if f["order_id"] == oid and f["symbol"] == s)
                          for s in syms})
            if o and float(o.get("filledQuantity") or 0) > min(this.values()) + 1e-9:
                return "UNKNOWN", "Schwab reports more closed than its execution details show yet"
        if not evidence_fresh or positions is None:
            return None, ""
        acct = {s: positions.get(s, 0.0) for s in syms}
        agrees = all(abs(acct[s] - expected[s]) < 1e-9 for s in syms)
        if e_status not in TERMINAL:
            if max(fe.values()) < 1e-9:
                return ("PENDING", "entry working") if agrees else ("BROKEN", _diff(acct, expected))
            return ("PARTIAL", f"{held_units:g} of {sp['qty']} filled, entry working") if agrees \
                else ("BROKEN", _diff(acct, expected))
        if max(fe.values()) < 1e-9:
            if e_status not in NOTHING_FILLED:
                return "UNKNOWN", f"entry {e_status.lower()} but no fill reported yet"
            return ("CANCELED", f"entry {e_status.lower()}, nothing filled") if agrees \
                else ("BROKEN", _diff(acct, expected))
        if exp and today > exp:
            return "EXPIRED_CHECK", (f"expired {exp.isoformat()}: check settlement or assignment before final P&L; "
                                     + _diff(acct, expected))
        if not agrees:
            return "BROKEN", _diff(acct, expected)
        if all(abs(v) < 1e-9 for v in expected.values()):
            if not (sp["close_order_id"] or sp.get("target_order_id")):
                return "BROKEN", "flat with no close order of its own"
            by_target = any(f["role"] == "target" and f["filled"] > 0 for f in sp["fills"])
            by_close = any(f["role"] == "close" and f["filled"] > 0 for f in sp["fills"])
            return "CLOSED", "closed by its own target" if by_target and not by_close else "closed by its own close order"
        if c_status and c_status not in TERMINAL:
            return "CLOSING", f"close working, {max(fc.values()):g} of {min(fe.values()):g} closed"
        if t_status and t_status not in TERMINAL:
            px, done = (target or {}).get("price"), max(fc.values())
            return "OPEN", (f"{held_units:g} spread(s) open, target working"
                            + (f" at {float(px):.2f}" if px is not None else "")
                            + (f" ({done:g} closed by it)" if done > 1e-9 else ""))
        return "OPEN", f"{held_units:g} spread(s) open"


JUST_SENT = 30.0        # seconds an order the companion just placed may be missing from Schwab's list


def _age_seconds(iso: str | None) -> float:
    if not iso:
        return float("inf")
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(iso)).total_seconds()
    except ValueError:
        return float("inf")


def _new_observation(obs, seen) -> bool:
    """A fresh look at the evidence: with a tuple (account read, order sweep, ...) every part
    must have been read again since the last counted disagreement."""
    if obs is None:
        return True
    if isinstance(obs, tuple) and isinstance(seen, tuple) and len(obs) == len(seen):
        return all(a != b for a, b in zip(obs, seen))
    return obs != seen


def _diff(acct: dict[str, float], expected: dict[str, float]) -> str:
    bad = [f"{s.strip()}: account {acct[s]:+g}, spread {expected[s]:+g}" for s in acct
           if abs(acct[s] - expected[s]) > 1e-9]
    return "account disagrees (" + "; ".join(bad) + "): closed or changed outside this spread, or assigned"
