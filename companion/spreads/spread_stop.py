"""App-watched spread stop (slice 4 of the spreads plan).

Schwab's API refuses every stop on a spread, so the only stop a spread can have is one the companion
watches. By default it only WATCHES: when the spread's net mid reaches the stop it shows and voices
"would stop out now", and logs it. With "Send when hit" (the user's opt-in per stop, their consent for that one
close, like Auto B/E) the fire also sends ONE close through the desk's tested Close path: claimed once
here (`claim_send`), never retried, only while the app runs. A used stop never re-arms by itself; setting
a stop again is a new consent.

Trigger (plan section 4b): the net mid from both legs' real-time bid / ask. Each leg quote must be valid
(bid <= ask, an ask above zero, the long leg's bid above zero), real time, under 15 s old by Schwab's own
quote time, after the spread was created, and the two legs' quote times within 10 s of each other. A credit
spread is at its stop when the cost to close (mid) rises to the stop; a debit spread when its value (mid)
falls to it. It must be at the stop on two DISTINCT Schwab observations (newer quote times) at least 2.5 s
apart; missing, stale or off-stop data resets the count. Regular hours only; an OPEN spread, or one whose close
is working (a resting close does not stop the watch).
"""
from __future__ import annotations

import time
from datetime import datetime

from ..store import Store, now_iso

SCHEMA = """
create table if not exists spread_stops (
    spread_id text primary key, price real, r real, set_at text, fired_at text, fired_mid real);
create table if not exists spread_stop_events (spread_id text, created text, kind text, mid real, detail text);
"""

MAX_AGE = 15.0      # seconds a leg quote may be old (Schwab's quote time)
SKEW = 10.0         # seconds the two legs' quote times may differ
GAP = 2.5           # seconds between the first and the confirming observation
STALE_GAP = 10.0    # a pending first hit older than this (a polling gap) no longer counts


def _ms(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


class StopWatch:
    """Per-spread watch-only stops on the companion's SQLite store."""

    def __init__(self, store: Store):
        self.store = store
        with store._lock, store._con:
            store._con.executescript(SCHEMA)
            cols = {r[1] for r in store._con.execute("pragma table_info(spread_stops)")}
            for col, kind in (("send", "integer default 0"), ("sent_at", "text"), ("sent_result", "text")):
                if col not in cols:
                    store._con.execute(f"alter table spread_stops add column {col} {kind}")
            # The app stopped mid-send: the close MAY be live. Fired with Send on but never sent: it never will be.
            store._con.execute("update spread_stops set sent_result = 'unclear: the app stopped while sending the close; "
                               "do NOT close again before checking thinkorswim' where sent_result = 'sending'")
            # Queued (fired, waiting for the order lock) but never started: nothing was sent.
            store._con.execute("update spread_stops set sent_result = 'refused: the app restarted before sending; "
                               "nothing was sent' where sent_result = 'queued'")
            store._con.execute("update spread_stops set sent_at = ?, sent_result = 'refused: the app restarted before "
                               "sending; nothing was sent' where send = 1 and fired_at is not null and sent_at is null",
                               (now_iso(),))
        # spread -> (count, last quote time, first quote time, the stop's set_at): a moved stop starts over
        self._obs: dict[str, tuple[int, float, float, str]] = {}

    def _event(self, sid: str, kind: str, mid: float | None = None, detail: str = "") -> None:
        with self.store._lock, self.store._con:
            self.store._con.execute("insert into spread_stop_events values (?,?,?,?,?)",
                                    (sid, now_iso(), kind, mid, detail[:300]))

    def get(self, sid: str) -> dict | None:
        with self.store._lock:
            r = self.store._con.execute("select price, r, set_at, fired_at, fired_mid, send, sent_at, sent_result "
                                        "from spread_stops where spread_id = ?", (sid,)).fetchone()
        if not r:
            return None
        out = dict(zip(("price", "r", "set_at", "fired_at", "fired_mid", "send", "sent_at", "sent_result"), r))
        out["send"] = bool(out["send"])
        return out

    SENDING = "select 1 from spread_stops where spread_id = ? and sent_result = 'sending'"

    def set(self, sid: str, price: float, r: float | None, send: bool = False) -> bool:
        """Watch this stop (replaces any earlier one, its would-fire and any earlier send). `send`: the user's
        consent to ONE close when it fires. False (nothing changed) while this stop's close is being sent:
        the claim and this check share the store lock, so a change either lands before the claim or is refused."""
        with self.store._lock, self.store._con:
            if self.store._con.execute(self.SENDING, (sid,)).fetchone():
                return False
            self.store._con.execute(
                "insert into spread_stops (spread_id, price, r, set_at, send) values (?,?,?,?,?) on conflict "
                "(spread_id) do update set price = excluded.price, r = excluded.r, set_at = excluded.set_at, "
                "send = excluded.send, fired_at = null, fired_mid = null, sent_at = null, sent_result = null",
                (sid, price, r, now_iso(), 1 if send else 0))
        self._obs.pop(sid, None)
        self._event(sid, "set", None, f"stop {price:.2f}" + (f" ({r:g}R)" if r is not None else "")
                    + (", send when hit" if send else ", watch only"))
        return True

    def claim_send(self, sid: str) -> str | None:
        """Claim this stop's ONE send, atomically (a fired stop with Send on that never sent): the claim
        time, or None. The claim is 'queued' (still removable) until `start_send` makes it 'sending'."""
        at = now_iso()
        with self.store._lock, self.store._con:
            n = self.store._con.execute("update spread_stops set sent_at = ?, sent_result = 'queued' where spread_id = ? "
                                        "and send = 1 and fired_at is not null and sent_at is null", (at, sid)).rowcount
        return at if n == 1 else None

    def start_send(self, sid: str, claim: str) -> bool:
        """Queued -> sending, atomically, under the order lock: False when the stop was removed, set again or
        switched to watch-only meanwhile (that consent is gone). After this, removing it is refused."""
        with self.store._lock, self.store._con:
            n = self.store._con.execute("update spread_stops set sent_result = 'sending' where spread_id = ? and "
                                        "sent_at = ? and sent_result = 'queued' and send = 1", (sid, claim)).rowcount
        return n == 1

    def record_send(self, sid: str, outcome: str, text: str, claim: str | None = None) -> None:
        """outcome: sent | unclear (the close MAY be live) | closed (nothing left) | refused (nothing sent).
        Only onto the stop that was claimed (a stop set again meanwhile keeps its own record)."""
        with self.store._lock, self.store._con:
            self.store._con.execute("update spread_stops set sent_result = ? where spread_id = ? and "
                                    "(? is null or sent_at = ?)", (f"{outcome}: {text}"[:300], sid, claim, claim))
        self._event(sid, "sent" if outcome == "sent" else f"send_{outcome}", None, text)

    def mark_unsent(self, sid: str, why: str) -> None:
        """The fire could not even start its send (the hook failed): used, and said so."""
        with self.store._lock, self.store._con:
            self.store._con.execute("update spread_stops set sent_at = ?, sent_result = ? where spread_id = ? and "
                                    "sent_at is null", (now_iso(), f"refused: {why}; nothing was sent"[:300], sid))
        self._event(sid, "send_refused", None, why)

    def clear(self, sid: str) -> bool:
        """Remove the stop. False (kept) while its close is being sent: too late to take it back."""
        with self.store._lock, self.store._con:
            if self.store._con.execute(self.SENDING, (sid,)).fetchone():
                return False
            self.store._con.execute("delete from spread_stops where spread_id = ?", (sid,))
        self._obs.pop(sid, None)
        self._event(sid, "cleared")
        return True

    def dismiss(self, sid: str) -> None:
        """The user saw the would-fire: keep watching the same stop. A stop that sent (or tried to) stays used."""
        with self.store._lock, self.store._con:
            self.store._con.execute("update spread_stops set fired_at = null, fired_mid = null where spread_id = ? "
                                    "and sent_at is null", (sid,))
        self._obs.pop(sid, None)
        self._event(sid, "dismissed")

    def events(self, sid: str) -> list[dict]:
        with self.store._lock:
            return [dict(zip(("created", "kind", "mid", "detail"), r)) for r in self.store._con.execute(
                "select created, kind, mid, detail from spread_stop_events where spread_id = ? order by rowid", (sid,))]

    # ---- the watch ----------------------------------------------------------------------------

    def watch(self, sp: dict, quote_of, market_open: bool, now: float | None = None) -> dict | None:
        """The stop's status for the dashboard, after one look at the legs' quotes. `quote_of(symbol)`
        returns the poller's raw Schwab quote entry. Fires (watch-only) on the second distinct
        observation at the stop."""
        s = self.get(sp["spread_id"])
        if not s:
            return None
        sid = sp["spread_id"]
        now = time.time() if now is None else now
        credit = sp["kind"].endswith("CREDIT")
        out = {**s, "watch_only": not s["send"], "mid": None, "status": "watching", "detail": ""}

        def pause(status: str, why: str) -> dict:
            self._obs.pop(sid, None)
            out.update(status=status, detail=why)
            return out

        near = next((l for l in sp["legs"] if (l["side"] == "SHORT") == credit), sp["legs"][0])
        far = next(l for l in sp["legs"] if l is not near)
        legs, why = [], None
        for l in (near, far):
            e = quote_of(l["symbol"]) or {}
            q = e.get("quote") or {}
            bid, ask, qt = q.get("bidPrice"), q.get("askPrice"), _ms(q.get("quoteTime"))
            if bid is None or ask is None or qt is None:
                why = why or f"no quote for {l['symbol'].strip()}"
            elif e.get("realtime") is not True:
                why = why or f"{l['symbol'].strip()}: quote not confirmed real time"
            elif bid > ask or ask <= 0 or bid < 0:
                why = why or f"{l['symbol'].strip()}: invalid market ({bid} x {ask})"
            elif l["side"] == "LONG" and bid <= 0:
                why = why or f"{l['symbol'].strip()}: the long leg has no bid"
            elif now - qt / 1000 > MAX_AGE:
                why = why or f"{l['symbol'].strip()}: quote {now - qt / 1000:.0f} s old"
            legs.append((bid, ask, qt))
        if why is None:
            out["mid"] = round((legs[0][0] + legs[0][1]) / 2 - (legs[1][0] + legs[1][1]) / 2, 4)
        if s["sent_at"]:
            out["status"] = "sent"
            return out
        if s["fired_at"]:
            out["status"] = "fired"
            return out
        if sp["state"] not in ("OPEN", "CLOSING"):
            return pause("paused", f"the spread is {sp['state']}")
        if not market_open:
            return pause("paused", "outside regular hours")
        if why:
            return pause("suspended", why)
        (_, _, qn), (_, _, qf) = legs
        if abs(qn - qf) > SKEW * 1000:
            return pause("suspended", "the legs' quotes are too far apart in time")
        try:
            created = datetime.fromisoformat(sp["created"]).timestamp() * 1000
        except (KeyError, TypeError, ValueError):
            created = None
        if created is None or min(qn, qf) < created:
            return pause("suspended", "waiting for quotes newer than the spread")
        mid = out["mid"]
        hit = mid >= s["price"] if credit else mid <= s["price"]
        if not hit:
            self._obs.pop(sid, None)
            gap = (s["price"] - mid) if credit else (mid - s["price"])
            out["detail"] = f"mid {mid:.2f}, {gap:.2f} from the stop"
            return out
        newest = max(qn, qf)
        n, last, first, rev = self._obs.get(sid, (0, 0.0, newest, s["set_at"]))
        if rev != s["set_at"] or (n and (newest - last) / 1000 > STALE_GAP):
            n, last, first, rev = 0, 0.0, newest, s["set_at"]     # a moved stop, or a gap in the watch: start over
        if newest > last:
            n, last = n + 1, newest
        self._obs[sid] = (n, last, first, rev)
        if n >= 2 and (last - first) / 1000 >= GAP:
            self._obs.pop(sid, None)
            with self.store._lock, self.store._con:
                # Only the stop this look evaluated: Set may have moved it meanwhile (another thread).
                done = self.store._con.execute(
                    "update spread_stops set fired_at = ?, fired_mid = ? where spread_id = ? and fired_at is null "
                    "and set_at = ? and price = ?", (now_iso(), mid, sid, s["set_at"], s["price"])).rowcount
            if not done:
                out["detail"] = "the stop changed: watching the new one"
                return out
            self._event(sid, "would_fire", mid, f"stop {s['price']:.2f}")
            out.update(self.get(sid) or {}, status="fired", fired_now=True)
            return out
        out["detail"] = f"mid {mid:.2f} at the stop: confirming ({n} of 2)"
        return out
