"""Local SQLite record of everything the companion sees (data/companion.db).

v1 only displays; v2 coaching needs history, so we keep evidence now:
- transactions and orders by their broker ids, with a new version row whenever
  the payload changes (corrections, status changes) instead of overwriting;
- position snapshots only when they change;
- quotes for open / ordered symbols on every quote poll (price path for
  sampled MFE/MAE later);
- one row per API request (endpoint, timing, outcome).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "companion.db"

SCHEMA = """
create table if not exists transactions (
    activity_id integer primary key, time text, order_id integer,
    hash text, payload text, first_seen text, last_seen text);
create table if not exists transaction_versions (
    activity_id integer, seen_at text, hash text, payload text);
create table if not exists orders (
    order_id integer primary key, entered text, status text,
    hash text, payload text, first_seen text, last_seen text);
create table if not exists order_versions (
    order_id integer, seen_at text, status text, hash text, payload text);
create table if not exists position_snapshots (
    seen_at text, hash text, payload text);
create table if not exists quotes (
    seen_at text, symbol text, bid real, ask real, last real, mark real,
    volume real, quote_time text);
create table if not exists requests (
    endpoint text, started text, ended text, ok integer, http integer, error text);
create table if not exists prev_session (
    day text, symbol text, qty real, basis real, seen_at text, primary key (day, symbol));
create table if not exists tickets (
    ticket_id text primary key, created text, symbol text, side text, qty integer,
    limit_price text, stop text, target text, risk real, status text, order_id integer,
    detail text, request text, updated text);
create table if not exists settings (key text primary key, value text);
create table if not exists actions (
    created text, kind text, symbol text, order_id integer, status text, detail text);
create table if not exists auto_be (
    ticket_id text primary key, symbol text, state text, created text, updated text, detail text);
create index if not exists ix_tx_time on transactions(time);
create index if not exists ix_quotes on quotes(symbol, seen_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _hash(obj) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True).encode()).hexdigest()


class Store:
    def __init__(self, path: Path = DB):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(path, check_same_thread=False)
        self._con.executescript(SCHEMA)
        self._lock = threading.Lock()
        self._last_positions_hash: str | None = None

    def _upsert(self, table: str, vtable: str, key: str, key_val, extra: dict, payload) -> None:
        h = _hash(payload)
        ts = now_iso()
        row = self._con.execute(f"select hash from {table} where {key}=?", (key_val,)).fetchone()
        body = json.dumps(payload)
        if row is None:
            cols = [key, *extra, "hash", "payload", "first_seen", "last_seen"]
            vals = [key_val, *extra.values(), h, body, ts, ts]
            self._con.execute(f"insert into {table} ({','.join(cols)}) values ({','.join('?' * len(cols))})", vals)
        elif row[0] != h:
            sets = ", ".join(f"{k}=?" for k in extra)
            self._con.execute(
                f"update {table} set {sets}{',' if sets else ''} hash=?, payload=?, last_seen=? where {key}=?",
                [*extra.values(), h, body, ts, key_val])
        else:
            self._con.execute(f"update {table} set last_seen=? where {key}=?", (ts, key_val))
            return
        vextra = {"status": extra["status"]} if "status" in extra else {}
        vcols = [key, "seen_at", *vextra, "hash", "payload"]
        self._con.execute(
            f"insert into {vtable} ({','.join(vcols)}) values ({','.join('?' * len(vcols))})",
            [key_val, ts, *vextra.values(), h, body])

    def save_transactions(self, txns: list[dict]) -> None:
        with self._lock, self._con:
            for t in txns:
                if t.get("activityId") is None:
                    continue
                self._upsert("transactions", "transaction_versions", "activity_id", t["activityId"],
                             {"time": t.get("time"), "order_id": t.get("orderId")}, t)

    def save_orders(self, orders: list[dict]) -> None:
        with self._lock, self._con:
            for o in orders:
                self._upsert("orders", "order_versions", "order_id", o["orderId"],
                             {"entered": o.get("enteredTime"), "status": o.get("status")}, o)

    def save_positions(self, account: dict) -> None:
        h = _hash(account)
        if h == self._last_positions_hash:
            return
        with self._lock, self._con:
            self._con.execute("insert into position_snapshots values (?,?,?)",
                              (now_iso(), h, json.dumps(account)))
        self._last_positions_hash = h

    def save_quotes(self, quotes: dict[str, dict]) -> None:
        ts = now_iso()
        rows = []
        for sym, q in quotes.items():
            qq = q.get("quote") or {}
            rows.append((ts, sym, qq.get("bidPrice"), qq.get("askPrice"), qq.get("lastPrice"),
                         qq.get("mark"), qq.get("totalVolume"), str(qq.get("quoteTime") or "")))
        with self._lock, self._con:
            self._con.executemany("insert into quotes values (?,?,?,?,?,?,?,?)", rows)

    def log_request(self, endpoint: str, started: str, ok: bool, http: int | None, error: str | None) -> None:
        with self._lock, self._con:
            self._con.execute("insert into requests values (?,?,?,?,?,?)",
                              (endpoint, started, now_iso(), int(ok), http, error))

    # ---- order tickets (the only records of what the companion sent) ----

    def log_action(self, kind: str, symbol: str | None, order_id: int | None, status: str,
                   detail: str | None = None) -> None:
        with self._lock, self._con:
            self._con.execute("insert into actions values (?,?,?,?,?,?)",
                              (now_iso(), kind, symbol, order_id, status, (detail or "")[:1000]))

    def actions_since(self, iso_utc: str) -> list[dict]:
        with self._lock:
            cur = self._con.execute("select created, kind, symbol, order_id, status, detail from actions "
                                    "where created >= ? order by created", (iso_utc,))
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._lock:
            row = self._con.execute("select value from settings where key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._lock, self._con:
            self._con.execute("insert or replace into settings values (?,?)", (key, value))

    def insert_ticket(self, t: dict) -> bool:
        """Persist intent BEFORE sending. False if this ticket id was already used."""
        ts = now_iso()
        try:
            with self._lock, self._con:
                self._con.execute(
                    "insert into tickets values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (t["ticket_id"], ts, t["symbol"], t["side"], t["qty"], t["limit"], t["stop"],
                     t["target"], t["risk"], "SENDING", None, None, json.dumps(t["request"]), ts))
        except sqlite3.IntegrityError:
            return False
        return True

    def update_ticket(self, ticket_id: str, status: str, order_id: int | None = None,
                      detail: str | None = None) -> None:
        with self._lock, self._con:
            self._con.execute(
                "update tickets set status=?, order_id=coalesce(?, order_id), detail=?, updated=? "
                "where ticket_id=?", (status, order_id, detail, now_iso(), ticket_id))

    def tickets_since(self, iso_utc: str) -> list[dict]:
        with self._lock:
            cur = self._con.execute(
                "select ticket_id, created, symbol, side, qty, limit_price, stop, target, risk, "
                "status, order_id, detail, request from tickets where created >= ? "
                "order by created desc", (iso_utc,))
            cols = [c[0] for c in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        for r in rows:
            try:
                r["request"] = json.loads(r["request"]) if r["request"] else None
            except ValueError:
                r["request"] = None
        return rows

    def open_tickets(self) -> list[dict]:
        """Tickets whose outcome or Schwab order id is not known: they block new submissions."""
        with self._lock:
            cur = self._con.execute(
                "select ticket_id, created, symbol, side, qty, status from tickets "
                "where status in ('SENDING', 'UNKNOWN', 'ACCEPTED_UNTRACKED')")
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def get_ticket(self, ticket_id: str) -> dict | None:
        with self._lock:
            cur = self._con.execute(
                "select ticket_id, created, symbol, side, qty, limit_price, stop, target, risk, "
                "status, order_id, detail from tickets where ticket_id=?", (ticket_id,))
            cols = [c[0] for c in cur.description]
            row = cur.fetchone()
        return dict(zip(cols, row)) if row else None

    # ---- auto B/E: one stop move per ticket, PENDING -> SENDING -> DONE | FAILED | SKIPPED ----

    def add_auto_be(self, ticket_id: str, symbol: str) -> None:
        ts = now_iso()
        with self._lock, self._con:
            self._con.execute("insert or ignore into auto_be values (?,?,?,?,?,?)",
                              (ticket_id, symbol, "PENDING", ts, ts, None))

    def auto_be(self, ticket_id: str) -> dict | None:
        with self._lock:
            cur = self._con.execute("select ticket_id, symbol, state, created, updated, detail "
                                    "from auto_be where ticket_id=?", (ticket_id,))
            cols = [c[0] for c in cur.description]
            row = cur.fetchone()
        return dict(zip(cols, row)) if row else None

    def set_auto_be(self, ticket_id: str, state: str, detail: str | None = None,
                    only_from: str | None = None) -> bool:
        """Change the state; with only_from, only if it still is that state (an atomic claim)."""
        with self._lock, self._con:
            cur = self._con.execute(
                "update auto_be set state=?, detail=?, updated=? where ticket_id=?"
                + (" and state=?" if only_from else ""),
                (state, (detail or "")[:1000] or None, now_iso(), ticket_id) + ((only_from,) if only_from else ()))
        return cur.rowcount == 1

    def claimed_order_ids(self) -> set[int]:
        with self._lock:
            rows = self._con.execute("select order_id from tickets where order_id is not null").fetchall()
        return {r[0] for r in rows}

    def all_orders(self) -> list[dict]:
        with self._lock:
            rows = self._con.execute("select payload from orders").fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_prev_session(self, day: str, rows: dict[str, tuple[float, float | None]]) -> None:
        """Yesterday's closing quantity per symbol, as first seen today. First sighting wins,
        so a later re-entry can never rewrite the basis of an earlier position."""
        ts = now_iso()
        with self._lock, self._con:
            self._con.executemany(
                "insert or ignore into prev_session values (?,?,?,?,?)",
                [(day, s, q, b, ts) for s, (q, b) in rows.items()])

    def load_prev_session(self, day: str) -> dict[str, tuple[float, float | None]]:
        with self._lock:
            rows = self._con.execute(
                "select symbol, qty, basis from prev_session where day=?", (day,)).fetchall()
        return {s: (q, b) for s, q, b in rows}

    def transactions_since(self, iso_utc: str) -> list[dict]:
        with self._lock:
            rows = self._con.execute(
                "select payload from transactions where time >= ? order by time", (iso_utc,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def latest_transaction_time(self) -> str | None:
        with self._lock:
            row = self._con.execute("select max(time) from transactions").fetchone()
        return row[0] if row else None
