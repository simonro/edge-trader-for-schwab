"""The one background poller. REST only (Schwab allows one streamer per user; another app may own it).

Request schedule, per minute (Schwab allows about 120 for everything, shared with any other app
using the same Schwab login):
    account + positions   every 5 s   12
    orders entered today  every 5 s   12
    orders, 59-day sweep  every 30 s   4   (status WORKING + PENDING_ACTIVATION: older
                                            GTC orders; an unfiltered 59-day query times out)
    transactions today    every 10 s   6
    quotes (batched)      every 3 s   20   (open positions and working orders, one request)
    transactions, 3 days  every 10 min     (late corrections to earlier sessions)
    order_details         as needed        (orders that drop out of the working set)
                                     ----
                                     ~54
    ticket quotes (outside this limiter) every 5 s while a ticket window has focus, else every
    30 s: up to ~12 more. Order actions are on top, at most one at a time.
Enforced, not just scheduled: every request goes through one limiter (MAX_PER_MIN),
schwabdev's internal retries are disabled (companion/schwab.py), and a 429 pauses
ALL companion requests. Each endpoint keeps its own last-success time.
"""
from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from . import schwab
from .orders import TERMINAL, pending_orders, terminal_ids
from .pnl import ET, fills_from_transactions, session_day
from .store import Store, now_iso
from .view import build_state

log = logging.getLogger("companion.poller")

LOOKBACK_DAYS = 60         # transaction history for FIFO basis of carried positions
TXN_CAP = 3000             # Schwab's max transactions per response
ORDER_SWEEP_DAYS = 59      # orders endpoint accepts at most ~60 days
SWEEP_STATUSES = ("WORKING", "PENDING_ACTIVATION")
MAX_PER_MIN = 70           # hard ceiling for this app, leaves room for other apps on the same login
DETAILS_PER_SWEEP = 3

INTERVALS = {"account": 5, "orders_today": 5, "orders_sweep": 30, "transactions": 10,
             "quotes": 3, "corrections": 600, "hours": 1800}


def _retry_after(value: str | None) -> float:
    if not value:
        return 30.0
    try:
        return max(1.0, float(value))
    except ValueError:
        try:
            return max(1.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return 30.0


class Poller:
    def __init__(self, store: Store | None = None):
        self.store = store or Store()
        self.client = None
        self.account_hash: str | None = None
        self.account: dict | None = None
        self.orders_today: dict[int, dict] = {}
        self.orders_sweep: dict[int, dict] = {}
        self.orders_followed: dict[int, dict] = {}   # final state of vanished orders
        self.followed_at: dict[int, float] = {}      # when each followed order was last asked
        self.quotes: dict = {}
        self.trail_best: dict[int, float] = {}   # trailing stop order id -> best price seen
        self.hours: dict = {}       # today's and the next regular session (Schwab, holidays and early closes)
        self.trader = None   # set by the app; the poller never sends orders (Auto B/E hands its one move to it)
        # Every order id ever seen finished. Schwab's WORKING sweep can return a frozen
        # stand-alone copy of an order that actually filled inside an OCO.
        self.terminal: set[int] = terminal_ids(self.store.all_orders())
        self.history_complete = False
        self.fresh: dict[str, dict] = {k: {"ok_at": None, "error": None, "interval": v}
                                       for k, v in INTERVALS.items()}
        self.fresh["backfill"] = {"ok_at": None, "error": None, "interval": None}
        self.state: dict = {"booting": True, "status": "starting"}
        self.status = "starting"
        self.last_rebuild: str | None = None
        self._next = {k: 0.0 for k in INTERVALS}
        self._sent: deque[float] = deque()
        self._cooldown_until = 0.0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, name="schwab-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            return self.state

    # ---- requests --------------------------------------------------------

    def _throttle(self) -> None:
        """Block until a request fits under MAX_PER_MIN and any 429 cooldown is over."""
        while not self._stop.is_set():
            now = time.monotonic()
            while self._sent and now - self._sent[0] > 60:
                self._sent.popleft()
            wait = max(self._cooldown_until - now,
                       (self._sent[0] + 60 - now) if len(self._sent) >= MAX_PER_MIN else 0)
            if wait <= 0:
                self._sent.append(now)
                return
            self._stop.wait(min(wait, 1.0))

    def note_external_request(self, retry_after=None) -> None:
        """Order traffic from the trader: counted in the same per-minute window (without blocking
        an order), and a 429 on it pauses the poller's own requests."""
        self._sent.append(time.monotonic())
        if retry_after is not None:
            self._cooldown_until = time.monotonic() + _retry_after(str(retry_after))

    def cooldown_left(self) -> float:
        return max(0.0, self._cooldown_until - time.monotonic())

    def _call(self, name: str, fn):
        """One HTTP request. Returns parsed JSON, or None on any failure (never raises)."""
        self._throttle()
        started = now_iso()
        fr = self.fresh.setdefault(name, {"ok_at": None, "error": None, "interval": None})
        try:
            r = fn()
            if r.status_code == 429:
                wait = _retry_after(r.headers.get("Retry-After"))
                self._cooldown_until = time.monotonic() + wait
                fr["error"] = f"rate limited, all requests paused {wait:.0f}s"
                self.store.log_request(name, started, False, 429, r.text[:300])
                return None
            if r.status_code != 200:
                fr["error"] = f"HTTP {r.status_code}"
                self.store.log_request(name, started, False, r.status_code, r.text[:300])
                return None
            data = r.json() if r.content else None
        except Exception as e:  # network, auth, permission, bad JSON
            fr["error"] = repr(e)[:200]
            self.store.log_request(name, started, False, None, repr(e)[:300])
            log.warning("%s failed: %r", name, e)
            return None
        self.store.log_request(name, started, True, 200, None)
        return data

    def _ok(self, name: str) -> None:
        """Mark an endpoint fresh only after its data was validated and stored."""
        self.fresh[name]["ok_at"] = now_iso()
        self.fresh[name]["error"] = None

    def _connect(self) -> bool:
        try:
            client = schwab.make_client()
        except Exception as e:
            self.status = f"auth: {e}"
            return False
        self.client = client
        accts = self._call("account", lambda: client.linked_accounts())
        if isinstance(accts, list) and len(accts) > 1:
            log.warning("%d linked accounts: the companion uses the first one", len(accts))
        if not accts or not isinstance(accts, list) or "hashValue" not in accts[0]:
            self.client = None   # retry discovery next time; never poll without an account
            self.status = "could not list linked accounts"
            return False
        self.account_hash = accts[0]["hashValue"]
        if self.trader is not None:
            self.trader.expected_hash = self.account_hash      # orders only to the account shown
        return True

    def _fetch_transactions(self, name: str, start: datetime, end: datetime) -> bool:
        """Store all TRADE transactions in [start, end); halve a range that hits the cap."""
        data = self._call(name, lambda: self.client.transactions(self.account_hash, start, end, "TRADE"))
        if data is None or not isinstance(data, list):
            return False
        if len(data) >= TXN_CAP and end - start > timedelta(hours=1):
            mid = start + (end - start) / 2
            return self._fetch_transactions(name, start, mid) and self._fetch_transactions(name, mid, end)
        self.store.save_transactions(data)
        return True

    def _backfill(self) -> bool:
        end = datetime.now(timezone.utc)
        t0 = end - timedelta(days=LOOKBACK_DAYS)
        while t0 < end and not self._stop.is_set():
            t1 = min(t0 + timedelta(days=10), end)
            if not self._fetch_transactions("backfill", t0, t1):
                self.fresh["backfill"]["error"] = "backfill incomplete, retrying"
                return False
            t0 = t1
        self._ok("backfill")
        return True

    def _session_start_utc(self) -> datetime:
        # Futures session for "today" opened at 18:00 ET yesterday; equities are inside it.
        et_midnight = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0)
        return (et_midnight - timedelta(hours=6)).astimezone(timezone.utc)

    def _poll(self, name: str) -> bool:
        h = self.account_hash
        now = datetime.now(timezone.utc)
        if name == "account":
            data = self._call(name, lambda: self.client.account_details(h, fields="positions"))
            if not isinstance(data, dict) or "securitiesAccount" not in data:
                return False
            self.account = data
            self.store.save_positions(data)
            self._record_prev_session(data)
        elif name == "orders_today":
            data = self._call(name, lambda: self.client.account_orders(h, self._session_start_utc(), now))
            if not isinstance(data, list):
                return False
            self.store.save_orders(data)
            self.terminal |= terminal_ids(data)
            self.orders_today = {o["orderId"]: o for o in data}
        elif name == "orders_sweep":
            found: dict[int, dict] = {}
            for status in SWEEP_STATUSES:
                data = self._call(name, lambda st=status: self.client.account_orders(
                    h, now - timedelta(days=ORDER_SWEEP_DAYS), now, status=st))
                if not isinstance(data, list):
                    return False
                found.update({o["orderId"]: o for o in data})
            self.store.save_orders(list(found.values()))
            self.terminal |= terminal_ids(list(found.values()))
            vanished = [oid for oid in self.orders_sweep
                        if oid not in found and oid not in self.orders_today]
            self.orders_sweep = found
            # Orders followed earlier while still live are asked again, oldest first, so a stop that
            # was cancelled later never keeps counting as protection.
            stale = sorted((oid for oid in self.orders_followed if oid not in found and oid not in self.orders_today),
                           key=lambda o: self.followed_at.get(o, 0.0))      # least recently asked first
            self._follow((vanished + stale)[:DETAILS_PER_SWEEP])
        elif name == "transactions":
            if not self._fetch_transactions(name, self._session_start_utc() - timedelta(hours=1), now):
                return False
        elif name == "hours":
            if not self._fetch_hours(name, now):
                return False
        elif name == "corrections":
            if not self._fetch_transactions(name, now - timedelta(days=3), now):
                return False
        elif name == "quotes":
            syms = self._quote_symbols()
            if syms:
                data = self._call(name, lambda: self.client.quotes(syms))
                if not isinstance(data, dict):
                    return False
                self.quotes = data
                self.store.save_quotes({k: v for k, v in data.items() if isinstance(v, dict)})
        self._ok(name)
        return True

    def _follow(self, order_ids: list[int]) -> None:
        """Orders that left the working set: fetch their final state once."""
        for oid in order_ids:
            self.followed_at[oid] = time.monotonic()
            data = self._call("order_details", lambda o=oid: self.client.order_details(self.account_hash, o))
            if isinstance(data, dict) and data.get("orderId"):
                self.store.save_orders([data])
                self.terminal |= terminal_ids([data])
                if data.get("status") not in TERMINAL:
                    self.orders_followed[oid] = data
                else:
                    self.orders_followed.pop(oid, None)

    def _record_prev_session(self, data: dict) -> None:
        day = session_day(datetime.now(timezone.utc), "EQUITY").isoformat()
        rows: dict[str, tuple[float, float | None]] = {}
        for p in (data.get("securitiesAccount") or {}).get("positions") or []:
            sym = (p.get("instrument") or {}).get("symbol")
            prev = float(p.get("previousSessionLongQuantity") or 0) - float(p.get("previousSessionShortQuantity") or 0)
            if not sym or abs(prev) < 1e-9:
                continue
            qty = float(p.get("longQuantity") or 0) - float(p.get("shortQuantity") or 0)
            untouched = abs(qty - prev) < 1e-9 and not float(p.get("currentDayCost") or 0)
            # Schwab's average describes yesterday's inventory only while nothing traded today.
            rows[sym] = (prev, float(p["averagePrice"]) if untouched and p.get("averagePrice") else None)
        if rows:
            self.store.save_prev_session(day, rows)

    def _quote_symbols(self) -> list[str]:
        syms: set[str] = set()
        acct = (self.account or {}).get("securitiesAccount") or {}
        for p in acct.get("positions") or []:
            s = (p.get("instrument") or {}).get("symbol")
            if s:
                syms.add(s)
        for o in self._merged_orders():
            if o.get("status") in TERMINAL or o.get("orderId") in self.terminal:
                continue
            for leg in o.get("orderLegCollection") or []:
                syms.add(leg["instrument"]["symbol"])
            for ch in o.get("childOrderStrategies") or []:
                for leg in ch.get("orderLegCollection") or []:
                    syms.add(leg["instrument"]["symbol"])
        return sorted(syms)[:200]

    def _merged_orders(self) -> list[dict]:
        merged = dict(self.orders_followed)
        merged.update(self.orders_sweep)
        merged.update(self.orders_today)   # the 5 s call is fresher
        return list(merged.values())

    def _fresh(self, name: str) -> bool:
        """Last call succeeded and is younger than 3 polling intervals."""
        f = self.fresh[name]
        if f["error"] or not f["ok_at"]:
            return False
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(f["ok_at"])).total_seconds()
        return age <= f["interval"] * 3

    def _fetch_hours(self, name: str, now: datetime) -> bool:
        """Today's regular session and the next one that has not ended (up to a week ahead, so a
        weekend or holiday costs a few calls every 30 min)."""
        today = now.astimezone(ET).date()
        out: dict = {"today": None, "next": None}
        for i in range(7):
            d = today + timedelta(days=i)
            data = self._call(name, lambda d=d: self.client.market_hours(["equity"], date=d.isoformat()))
            if not isinstance(data, dict):
                return False
            eq = next(iter((data.get("equity") or {}).values()), {})
            reg = (eq.get("sessionHours") or {}).get("regularMarket") or []
            sess = {"date": d.isoformat(), "open": reg[0]["start"], "close": reg[-1]["end"]}                 if eq.get("isOpen") and reg else None
            if i == 0:
                out["today"] = sess
            if sess and datetime.fromisoformat(sess["close"]) > now:
                out["next"] = sess
                break
        self.hours = out
        return True

    def _orders_fresh(self) -> bool:
        return self._fresh("orders_today") and self._fresh("orders_sweep")

    # ---- loop ------------------------------------------------------------

    def _rebuild(self) -> None:
        since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).strftime("%Y-%m-%dT%H:%M:%S")
        day = session_day(datetime.now(timezone.utc), "EQUITY").isoformat()
        txns = self.store.transactions_since(since)
        st = build_state(account=self.account, orders=self._merged_orders(),
                         txns=txns, quotes=self.quotes,
                         prev_session=self.store.load_prev_session(day),
                         freshness=self.fresh, history_complete=self.history_complete,
                         orders_fresh=self._orders_fresh(), auth_hours_left=schwab.auth_hours_left(),
                         known_terminal=self.terminal)
        self.last_rebuild = now_iso()
        st["status"] = self.status
        st["last_rebuild"] = self.last_rebuild
        st["market_hours"] = self.hours
        if st.get("summary") and not self._fresh("transactions"):
            st["summary"]["realized"]["complete"] = False
            st["summary"]["realized"]["stale"] = True
        if self.trader is not None:
            today = list(self.orders_today.values())
            self.trader.reconcile(today)
            positions = {}
            for p in ((self.account or {}).get("securitiesAccount") or {}).get("positions") or []:
                sym = (p.get("instrument") or {}).get("symbol")
                if sym:
                    positions[sym] = float(p.get("longQuantity") or 0) - float(p.get("shortQuantity") or 0)
            st["trading"] = self.trader.status()
            st["brackets"] = self.trader.brackets(
                today, pending_orders(self._merged_orders(), self.terminal), positions,
                self._orders_fresh() and self._fresh("account"))
            self._add_r_and_log(st, today, txns)
            self._auto_be(st)
        self._trail_estimates(st)
        with self._lock:
            self.state = st

    def _trail_estimates(self, st: dict) -> None:
        """Where each Schwab trailing stop sits now. Schwab's own stop price when it reports one;
        otherwise the best mark seen since we first saw the order, minus the offset. Seen from
        polls only, so the estimate never runs ahead of the real stop (it can lag behind it)."""
        from decimal import Decimal
        from .bracket import trail_level
        live_ids = set()
        for p in st.get("positions") or []:
            t = p.get("trail")
            if not t:
                continue
            live_ids.add(t["order_id"])
            raw = (self.quotes.get(p["symbol"]) or {}).get("quote") or {}
            px = {"MARK": raw.get("mark") or raw.get("lastPrice"), "LAST": raw.get("lastPrice"),
                  "BID": raw.get("bidPrice"), "ASK": raw.get("askPrice")}.get((t.get("basis") or "MARK").upper())
            # A quote from before the order was entered could be a better price the trail never
            # saw: only quotes stamped after entry count.
            qt = raw.get("tradeTime") if (t.get("basis") or "MARK").upper() == "LAST" else raw.get("quoteTime")
            try:
                entered = datetime.strptime(t.get("entered") or "", "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1000
            except ValueError:
                entered = None
            if entered is None or not qt or qt < entered:
                px = None
            best = self.trail_best.get(t["order_id"])
            if px:
                better = best is None or (px > best if p["side"] == "LONG" else px < best)
                best = px if better else best
                self.trail_best[t["order_id"]] = best
            if t.get("schwab_stop") is not None:
                t.update({"level": t["schwab_stop"], "estimated": False, "best": best})
            elif best and t.get("offset") and t.get("whole"):
                spec = {"offset": Decimal(str(t["offset"])), "link": t.get("link") or "VALUE"}
                lvl = float(trail_level(Decimal(str(best)), spec, p["side"]))
                t.update({"level": lvl, "estimated": True, "best": best})
                p["stop_price"] = lvl          # ruler and P&L at stop use the estimate, flagged
                if p.get("avg_price"):
                    d = 1 if p["side"] == "LONG" else -1
                    p["pnl_at_stop"] = (lvl - p["avg_price"]) * p["qty"] * d * (p.get("multiplier") or 1)
        for oid in list(self.trail_best):
            if oid not in live_ids:
                del self.trail_best[oid]

    def _auto_be(self, st: dict) -> None:
        """Tickets sent with Auto B/E: show where each one fires, and when the mark touches +1R
        (frozen R of the ticket that opened the position) claim it and hand the one stop move to the
        trader in its own thread. The poller itself never waits on an order."""
        from .trading import market_open
        live = (self._fresh("quotes") and self._fresh("account") and self._fresh("transactions")
                and self._orders_fresh() and market_open())
        now_ms = time.time() * 1000
        for p in st.get("positions") or []:
            r = p.get("r")
            ab = self.trader.store.auto_be(r["ticket_id"]) if r else None
            if not ab:
                continue
            long = p["side"] == "LONG"
            trigger = round(r["entry"] + r["rps"] if long else r["entry"] - r["rps"], 4)
            p["auto_be"] = {"state": ab["state"], "trigger": trigger, "detail": ab["detail"]}
            if ab["state"] != "PENDING" or not live:
                continue
            entry = self.quotes.get(p["symbol"]) or {}
            q = entry.get("quote") or {}
            mark = q.get("mark")
            if entry.get("realtime") is False:
                continue
            try:   # only a real-time quote from after the entry fill and under 15 s old counts
                filled = datetime.strptime(r.get("first_fill") or "", "%Y-%m-%dT%H:%M:%S%z").timestamp() * 1000
            except ValueError:
                continue
            qt = q.get("quoteTime")
            if not mark or not qt or qt < filled or now_ms - qt > 15_000:
                continue
            if mark < trigger - 1e-9 if long else mark > trigger + 1e-9:
                continue
            if not self.trader.store.set_auto_be(r["ticket_id"], "SENDING", f"mark {mark:g} touched +1R {trigger:g}",
                                                 only_from="PENDING"):
                continue
            p["auto_be"]["state"] = "SENDING"
            threading.Thread(target=self.trader.auto_breakeven, name=f"auto-be-{p['symbol']}", daemon=True,
                             args=(r["ticket_id"], p["symbol"], p["side"], r["entry"],
                                   self._merged_orders(), set(self.terminal))).start()

    def _add_r_and_log(self, st: dict, orders_today: list[dict], txns: list[dict]) -> None:
        """Live R on each position (frozen basis from the companion ticket) and a per-symbol
        action log: tickets sent, cancels/closes requested and confirmed, and fills."""
        basis = self.trader.r_basis(orders_today)

        def owns(b: dict, p: dict) -> bool:
            # The ticket's entry fill must be what opened the CURRENT position (same flat-to-flat
            # trade). A later reopen in thinkorswim must not inherit an old trade's R.
            if not (b.get("first_fill") and p.get("opened")):
                return False
            try:
                a = datetime.strptime(b["first_fill"], "%Y-%m-%dT%H:%M:%S%z")
                o = datetime.fromisoformat(p["opened"])
            except ValueError:
                return False
            return abs((a - o).total_seconds()) <= 5

        for p in st.get("positions") or []:
            b = basis.get(p["symbol"])
            if b and b["side"] == p["side"] and p.get("last") and owns(b, p):
                direction = 1 if p["side"] == "LONG" else -1
                p["r"] = {**b, "live_r": (float(p["last"]) - b["entry"]) * direction / b["rps"]}
            else:
                p["r"] = None
        start = datetime.now(ET).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
        log_by: dict[str, list[dict]] = {}

        def add(sym, when, text, kind):
            log_by.setdefault(sym, []).append({"t": when, "text": text, "kind": kind})

        for t in self.trader.store.tickets_since(start.isoformat()):
            add(t["symbol"], t["created"], f"Ticket {t['side']} {t['qty']} @ {t['limit_price']} "
                f"stop {t['stop']} target {t['target']}: {t['status']}", "ticket")
        for a in self.store.actions_since(start.isoformat()):
            if a["symbol"]:
                kind = "auto B/E" if a["kind"] == "auto_be" else a["kind"].replace("_", " ")
                add(a["symbol"], a["created"], f"{kind} "
                    f"{a['order_id'] or ''}: {a['status']} {a['detail'] or ''}".strip(), "action")
        today = session_day(datetime.now(timezone.utc), "EQUITY")
        for f in fills_from_transactions(txns):
            if f.asset_type != "FUTURE" and f.day == today:
                add(f.symbol, f.time.isoformat(), f"Fill {'bought' if f.qty > 0 else 'sold'} "
                    f"{abs(f.qty):g} @ {f.price:g}", "fill")
        for rows in log_by.values():
            rows.sort(key=lambda r: r["t"])
        st["action_log"] = log_by

    def _run(self) -> None:
        backfill_next = 0.0
        while not self._stop.is_set():
            try:
                if self.client is None:
                    if not self._connect():
                        self._publish_status()
                        self._stop.wait(30)
                        continue
                    self.status = "loading 60 days of fills"
                    self._publish_status()

                if not self.history_complete and time.monotonic() >= backfill_next:
                    self.history_complete = self._backfill()
                    backfill_next = time.monotonic() + 60
                self.status = "live" if self.history_complete else "live, fill history incomplete"

                left = schwab.auth_hours_left()
                if left is None or left < schwab.SAFETY_HOURS:
                    # Keep serving cached data, clearly stale; never trigger an interactive login.
                    self.status = "Schwab login expiring: run login.bat (python -m companion.login)"
                    self.client = None
                    self._publish_status()
                    self._stop.wait(60)
                    continue

                changed = False
                for name, every in INTERVALS.items():
                    if time.monotonic() >= self._next[name]:
                        self._next[name] = time.monotonic() + every
                        try:
                            changed |= self._poll(name)
                        except Exception as e:
                            self.fresh[name]["error"] = repr(e)[:200]
                            log.exception("poll %s failed", name)
                if changed:
                    try:
                        self._rebuild()
                    except Exception as e:
                        log.exception("rebuild failed")
                        self.status = f"display not updating: {e!r}"[:200]
                        self._publish_status()
            except Exception as e:   # the thread must never die silently
                log.exception("poller loop error")
                self.status = f"poller error: {e!r}"[:200]
                self._publish_status()
                self._stop.wait(5)
            self._stop.wait(0.5)

    def _publish_status(self) -> None:
        with self._lock:
            st = dict(self.state)
            st["status"] = self.status
            st["auth_hours_left"] = schwab.auth_hours_left()
            st["freshness"] = self.fresh
            st["last_rebuild"] = self.last_rebuild
            self.state = st
