"""Sync with the trading journal (Trading Journal AI), only on the trader's click.

Off until a journal URL is set. Nothing here places, changes or cancels orders, and nothing
calls Schwab: everything sent comes from what the poller already stored (fills from
/transactions, orders, tickets, the action log) plus the trader's own journal entries.

What a Sync sends (the journal's POST /api/sync/edge-trader):
- the raw Schwab TRADE transactions since the last sync (minus 3 days of overlap; the journal
  recognises fills it already has, from an earlier sync or from a statement CSV),
- per Edge Trader trade an "annotation": the trader's picks (strategy, source, tags, emotion,
  mistakes, reasons) and facts (initial stop, target, risk $, planned R:R, realized R),
  anchored to the trade's Schwab fill ids,
- events: stop and target set / moved / cancelled (from Schwab's order history, so changes made
  in thinkorswim count too), and the trader's timestamped notes,
- day notes.

Every Sync is two steps: Preview (a dry run: the journal computes exactly what it would do and
returns a state token) and Apply (sends the SAME payload with that token; the journal refuses
if anything changed in between).
"""
from __future__ import annotations

import ipaddress
import json
import re
import sqlite3
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from ..pnl import build_book, fills_from_transactions

ET = ZoneInfo("America/New_York")
SCHEMA = """
create table if not exists journal_annotations (
    id text primary key, symbol text, fill_keys text, fields text, tags text,
    created text, updated text);
create table if not exists journal_notes (
    id text primary key, annotation_id text, ts text, text text, updated text);
create table if not exists journal_day_notes (
    date text primary key, text text, updated text);
create table if not exists journal_tombstones (
    id text primary key, annotation_id text, deleted text);
create table if not exists journal_sync_log (
    id integer primary key autoincrement, at text, dry_run integer, ok integer,
    window_start text, summary text, error text);
"""
TEXT_FIELDS = ("strategy", "idea_source", "emotional_state", "mistakes", "entry_reason", "exit_reason")
TAG_TYPES = ("setup", "execution", "mistake", "emotion", "outcome")
INSTRUMENTS = {"EQUITY": "STOCK", "COLLECTIVE_INVESTMENT": "STOCK", "OPTION": "OPTION", "FUTURE": "FUTURE"}
OVERLAP_DAYS = 3
PREVIEW_TTL = 600          # seconds a preview stays applicable
TIMEOUT = 30
APPLY_TIMEOUT = 120        # a first Apply can carry weeks of fills

DEFAULTS = {"url": "", "allow_remote": False, "routes": {}, "dedup_accounts": [], "exclude_symbols": [],
            "first_sync_start": None, "last_sync_day": None, "last_sync_at": None}

STOP_TYPES = ("STOP", "STOP_LIMIT", "TRAILING_STOP", "TRAILING_STOP_LIMIT")
EXIT_LONG = ("SELL", "SELL_TO_CLOSE", "SELL_SHORT_EXEMPT")
EXIT_SHORT = ("BUY_TO_COVER", "BUY", "BUY_TO_CLOSE")


class JournalError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_local_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """The journal never redirects: a redirect could send Edge Trader to another server than the one the
    user approved, so it is refused (it comes back as an HTTP error with the redirect's status)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f"redirect to another address refused ({newurl})",
                                     headers, fp)


_OPENER = urllib.request.build_opener(_NoRedirect)


def _symbol_of(txn: dict) -> str | None:
    """The underlying / ticker a Schwab transaction trades, the way the journal reads it for its exclusion
    list (an option's underlying, a future's root, else the symbol)."""
    for leg in _legs(txn):
        ins = leg.get("instrument") or {}
        if ins.get("assetType") == "OPTION":
            return (ins.get("underlyingSymbol") or "").lstrip("$").upper() or None
        if ins.get("assetType") == "FUTURE":
            return (ins.get("symbol") or "").split(":")[0].upper() or None
        return (ins.get("symbol") or "").upper() or None
    return None


def _legs(txn: dict) -> list[dict]:
    return [i for i in (txn.get("transferItems") or [])
            if (i.get("instrument") or {}).get("assetType") not in (None, "CURRENCY")]


def _excluded(txn: dict, excluded: set[str]) -> bool:
    sym = _symbol_of(txn)
    return bool(sym) and any(sym == x or (x.startswith("/") and sym.startswith(x)) for x in excluded)


# A Schwab transaction as it goes to the journal: what the journal reads, without the account number.
_TXN_DROP = ("accountNumber",)


def _for_journal(txn: dict) -> dict:
    return {k: v for k, v in txn.items() if k not in _TXN_DROP}


def aid(fill_id: str) -> str:
    """Edge Trader fill id '<activityId>:<item index>' -> the journal's fill key (activityId)."""
    return str(fill_id).split(":", 1)[0]


def _session_day(txn: dict):
    """The trading day Schwab books a fill to (its tradeDate, midnight ET), or None."""
    d = _parse(txn.get("tradeDate"))
    return d.astimezone(ET).date() if d else None


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    s = str(ts).replace("Z", "+00:00")
    if len(s) > 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


ROLE = {"stop_set": "stop", "stop_moved": "stop", "breakeven": "stop", "trail": "stop", "stop_cancelled": "stop",
        "target_set": "target", "target_moved": "target", "target_cancelled": "target"}
REBUILD_GAP = 3.0   # seconds: thinkorswim and the companion rebuild a whole OCO on every change


def _collapse(events: list[dict]) -> list[dict]:
    """Drop the noise of OCO rebuilds: a cancel followed within a few seconds by a new order of the
    same role, and a 'move' to the level that role already had. Real moves and final cancels stay."""
    events = sorted(events, key=lambda e: (e["ts"], e["id"]))
    out, level = [], {}
    for i, e in enumerate(events):
        role = ROLE.get(e["kind"])
        if role and e["kind"].endswith("_cancelled"):
            t = _parse(e["ts"])
            replaced = any(ROLE.get(n["kind"]) == role and not n["kind"].endswith("_cancelled")
                           and (_parse(n["ts"]) - t).total_seconds() <= REBUILD_GAP
                           for n in events[i + 1:] if _parse(n["ts"]) and t)
            if replaced:
                continue
            level.pop(role, None)
        elif role and e["kind"] != "trail":
            prev = level.get(role)
            if prev is not None and e.get("price") is not None and abs(prev - e["price"]) < 1e-9:
                continue
            if prev is None and e["kind"].endswith("_moved") and not any(ROLE.get(o["kind"]) == role for o in out):
                e = {**e, "kind": f"{role}_set"}
            level[role] = e.get("price")
        out.append(e)
    return out


def _net_fill(order: dict) -> float | None:
    """A multi-leg order's average net fill price per spread from Schwab's execution legs (buys
    minus sells, by leg ratio), or None when Schwab lists no executions yet."""
    legs = {l.get("legId"): l for l in order.get("orderLegCollection") or []}
    qty_px: dict = {}
    for act in order.get("orderActivityCollection") or []:
        for x in act.get("executionLegs") or []:
            q, p = float(x.get("quantity") or 0), x.get("price")
            if q > 0 and p is not None:
                a = qty_px.setdefault(x.get("legId"), [0.0, 0.0])
                a[0] += q
                a[1] += q * float(p)
    if not legs or set(qty_px) != set(legs):
        return None
    base = min(float(l.get("quantity") or 0) for l in legs.values()) or 1.0
    net = 0.0
    for lid, l in legs.items():
        q, cost = qty_px[lid]
        sign = 1.0 if str(l.get("instruction") or "").startswith("BUY") else -1.0
        net += sign * (cost / q) * (float(l.get("quantity") or 0) / base)
    return round(abs(net), 4)


class Journal:
    def __init__(self, store, demo: bool = False):
        self.store = store
        self.demo = demo
        with store._lock, store._con:
            store._con.executescript(SCHEMA)
        self._lock = threading.Lock()          # one preview / apply at a time
        self._pending: dict | None = None      # {"token", "payload", "at", "result"}
        self._library: tuple[float, dict] | None = None

    # ---- settings -------------------------------------------------------

    def settings(self) -> dict:
        try:
            saved = json.loads(self.store.get_setting("journal") or "{}")
        except ValueError:
            saved = {}
        return {**DEFAULTS, **saved}

    def save_settings(self, body: dict) -> dict:
        with self._lock:                # never while a preview is being built
            return self._save_settings(body)

    def _save_settings(self, body: dict) -> dict:
        cur = self.settings()
        if "url" in body:
            url = str(body["url"] or "").strip().rstrip("/")
            if url:
                p = urllib.parse.urlparse(url)
                if p.scheme not in ("http", "https") or not p.hostname or p.path not in ("", "/"):
                    raise JournalError("Enter the journal's address, like http://127.0.0.1:8010")
                if not is_local_url(url) and not body.get("allow_remote"):
                    raise JournalError("That address is not on this computer. Tick 'Allow a journal on "
                                       "another computer' if you mean it: trades and notes will be sent there.")
            cur["url"] = url
            cur["allow_remote"] = bool(body.get("allow_remote")) and bool(url) and not is_local_url(url)
            self._library = None
        if "routes" in body:
            routes = {}
            for k, v in (body["routes"] or {}).items():
                if k in ("STOCK", "OPTION", "FUTURE") and v not in (None, ""):
                    routes[k] = int(v)
            cur["routes"] = routes
        if "dedup_accounts" in body:
            cur["dedup_accounts"] = sorted({int(a) for a in body["dedup_accounts"] or []})
        if "exclude_symbols" in body:
            raw = body["exclude_symbols"]
            items = raw.split(",") if isinstance(raw, str) else (raw or [])
            cur["exclude_symbols"] = sorted({str(s).strip().upper() for s in items if str(s).strip()})
        if "first_sync_start" in body:
            d = body["first_sync_start"]
            if d:
                datetime.strptime(d, "%Y-%m-%d")
            cur["first_sync_start"] = d or None
        self.store.set_setting("journal", json.dumps(cur))
        self._pending = None
        return cur

    # ---- per-trade account picked in the preview ------------------------

    def overrides(self) -> dict[str, int]:
        try:
            return {aid(k): int(v) for k, v in json.loads(self.store.get_setting("journal_overrides") or "{}").items()}
        except (ValueError, TypeError, AttributeError):
            return {}

    def set_account(self, fill_keys: list[str], account_id) -> dict:
        """Send these fills to this journal account instead of their route (None: back to the route)."""
        keys = {aid(k) for k in fill_keys if str(k).strip()}
        if not keys or len(keys) > 500:
            raise JournalError("No fills given." if not keys else "Too many fills in one pick.")
        acct = None if account_id in (None, "") else int(account_id)
        with self._lock:              # never while a preview is being built (it would keep the old picks)
            # The pick (or its removal) covers each whole round trip the given fills belong to.
            try:
                trades, _ = self._trades(self.window_start())
            except JournalError:
                trades = []
            for t, _fl in trades:
                ids = {aid(f) for f in t.fills}
                if ids & keys:
                    keys |= ids
            cur = self.overrides()
            for k in keys:
                if acct is None:
                    cur.pop(k, None)
                else:
                    cur[k] = acct
            self.store.set_setting("journal_overrides", json.dumps(cur))
            self._pending = None
        return {"overrides": len(cur)}

    def configured(self) -> bool:
        s = self.settings()
        return bool(s["url"]) and bool(s["routes"])

    # ---- HTTP -----------------------------------------------------------

    def _http(self, method: str, path: str, body=None, url: str | None = None, timeout: float = TIMEOUT):
        base = url or self.settings()["url"]
        if not base:
            raise JournalError("No journal address set (Journal settings).")
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with _OPENER.open(req, timeout=timeout) as r:
                raw = r.read().decode()
            try:
                return r.status, json.loads(raw or "null")
            except ValueError:
                raise JournalError(f"The address {base} answered, but not like the journal's API. Check the address.")
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read().decode() or "null")
            except ValueError:
                detail = None
            return e.code, detail
        except (TimeoutError, urllib.error.URLError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(e, TimeoutError) or isinstance(reason, TimeoutError) or "timed out" in str(reason):
                raise JournalError(f"No answer from the journal within {timeout:g} s. If this was an Apply it may "
                                   "still have gone through: press Sync again, the preview shows what is already "
                                   "there (nothing is added twice).")
            raise JournalError(f"Cannot reach the journal at {base}: {reason}")

    def test(self, url: str | None = None, allow_remote: bool = False) -> dict:
        if url:
            p = urllib.parse.urlparse(url)
            if p.scheme not in ("http", "https") or not p.hostname or p.path not in ("", "/"):
                raise JournalError("Enter the journal's address, like http://127.0.0.1:8010")
            if not is_local_url(url) and not allow_remote:
                raise JournalError("That address is not on this computer. Tick 'Allow a journal on another "
                                   "computer' first if you mean it: trades and notes will be sent there.")
        code, info = self._http("GET", "/api/sync/info", url=url)
        if code == 404:
            raise JournalError("This journal has no Edge Trader sync yet. Update Trading Journal AI.")
        if code != 200 or not isinstance(info, dict) or info.get("api") != "edge-trader-sync":
            raise JournalError(f"Unexpected answer from the journal (HTTP {code}).")
        if int(info.get("api_version") or 0) < 1:
            raise JournalError("The journal's sync version is too old. Update Trading Journal AI.")
        code, accounts = self._http("GET", "/api/accounts", url=url)
        if code != 200 or not isinstance(accounts, list):
            raise JournalError(f"Could not read the journal's accounts (HTTP {code}).")
        out = []
        for a in accounts:
            try:
                out.append({"id": int(a.get("id")), "name": str(a.get("name") or "")[:120]})
            except (AttributeError, TypeError, ValueError):
                continue                      # not an account the sync can route to
        return {"info": {"api": info.get("api"), "api_version": int(info.get("api_version") or 0),
                         "journal_version": str(info.get("journal_version") or "unknown")[:40]},
                "accounts": out}

    def library(self) -> dict:
        if self._library and time.time() - self._library[0] < 60:
            return self._library[1]
        code, lib = self._http("GET", "/api/library")
        if code != 200 or not isinstance(lib, dict):
            raise JournalError(f"Could not read the journal's library (HTTP {code}).")
        out = {"strategies": [i["name"] for i in lib.get("strategies") or []],
               "sources": [i["name"] for i in lib.get("sources") or []],
               "tags": {t: [i["name"] for i in (lib.get("tags") or {}).get(t) or []] for t in TAG_TYPES}}
        self._library = (time.time(), out)
        return out

    # ---- local journal entries ------------------------------------------

    def _row(self, sql, args=()):
        with self.store._lock:
            cur = self.store._con.execute(sql, args)
            cols = [c[0] for c in cur.description]
            r = cur.fetchone()
        return dict(zip(cols, r)) if r else None

    def _rows(self, sql, args=()):
        with self.store._lock:
            cur = self.store._con.execute(sql, args)
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def _write(self, sql, args=()):
        with self.store._lock, self.store._con:
            self.store._con.execute(sql, args)

    @staticmethod
    def annotation_id(fill_ids: list[str]) -> str:
        if not fill_ids:
            raise JournalError("This trade has no Schwab fill yet: wait a few seconds and try again.")
        return "t" + aid(fill_ids[0])

    def _find(self, fill_ids: list[str]) -> tuple[str, dict | None]:
        """The stored entry for a trade: by id (first fill), else any entry sharing one of its fills
        (two views of one carried position can disagree on its first fill)."""
        ann_id = self.annotation_id(fill_ids)
        row = self._row("select * from journal_annotations where id=?", (ann_id,))
        if row:
            return ann_id, row
        keys = {aid(f) for f in fill_ids}
        for r in self._rows("select * from journal_annotations order by created"):
            if keys & set(json.loads(r["fill_keys"] or "[]")):
                return r["id"], r
        return ann_id, None

    def get_entry(self, fill_ids: list[str]) -> dict:
        ann_id, row = self._find(fill_ids)
        notes = self._rows("select id, ts, text from journal_notes where annotation_id=? order by ts", (ann_id,))
        return {"id": ann_id,
                "fields": json.loads(row["fields"]) if row and row["fields"] else {},
                "tags": json.loads(row["tags"]) if row and row["tags"] else {},
                "notes": notes, "updated": row["updated"] if row else None}

    def save_entry(self, symbol: str, fill_ids: list[str], fields: dict | None, tags: dict | None) -> dict:
        ann_id, row = self._find(fill_ids)
        cur_fields = json.loads(row["fields"]) if row and row["fields"] else {}
        cur_tags = json.loads(row["tags"]) if row and row["tags"] else {}
        if fields is not None:
            for k, v in fields.items():
                if k not in TEXT_FIELDS:
                    raise JournalError(f"Unknown field {k}.")
                v = (str(v).strip()[:2000] if v is not None else "")
                if v:
                    cur_fields[k] = v
                else:
                    cur_fields.pop(k, None)
        if tags is not None:
            for t, vals in tags.items():
                if t not in TAG_TYPES:
                    raise JournalError(f"Unknown tag type {t}.")
                cur_tags[t] = sorted({str(v).strip()[:100] for v in vals or [] if str(v).strip()})
        keys = sorted(set(json.loads(row["fill_keys"]) if row else []) | {aid(f) for f in fill_ids})
        ts = now_iso()
        self._write("insert into journal_annotations values (?,?,?,?,?,?,?) on conflict(id) do update set "
                    "symbol=excluded.symbol, fill_keys=excluded.fill_keys, fields=excluded.fields, "
                    "tags=excluded.tags, updated=excluded.updated",
                    (ann_id, symbol, json.dumps(keys), json.dumps(cur_fields), json.dumps(cur_tags), ts, ts))
        return self.get_entry(fill_ids)

    def add_note(self, symbol: str, fill_ids: list[str], text: str) -> dict:
        text = str(text or "").strip()[:5000]
        if not text:
            raise JournalError("Type a note first.")
        self.save_entry(symbol, fill_ids, None, None)
        ann_id, _ = self._find(fill_ids)
        ts = now_iso()
        self._write("insert into journal_notes values (?,?,?,?,?)",
                    ("n" + secrets.token_hex(8), ann_id, ts, text, ts))
        return self.get_entry(fill_ids)

    def edit_note(self, note_id: str, text: str | None) -> None:
        note = self._row("select annotation_id from journal_notes where id=?", (note_id,))
        if not note:
            raise JournalError("That note no longer exists.")
        ts = now_iso()
        with self.store._lock, self.store._con:
            if text is None or not str(text).strip():
                self.store._con.execute("delete from journal_notes where id=?", (note_id,))
                self.store._con.execute("insert or replace into journal_tombstones values (?,?,?)",
                                        (note_id, note["annotation_id"], ts))
            else:
                self.store._con.execute("update journal_notes set text=?, updated=? where id=?",
                                        (str(text).strip()[:5000], ts, note_id))
            self.store._con.execute("update journal_annotations set updated=? where id=?", (ts, note["annotation_id"]))

    def day_note(self, date: str) -> dict:
        row = self._row("select * from journal_day_notes where date=?", (date,))
        return {"date": date, "text": row["text"] if row else "", "updated": row["updated"] if row else None}

    def save_day_note(self, date: str, text: str) -> dict:
        datetime.strptime(date, "%Y-%m-%d")
        self._write("insert into journal_day_notes values (?,?,?) on conflict(date) do update set "
                    "text=excluded.text, updated=excluded.updated", (date, str(text or "")[:50000], now_iso()))
        return self.day_note(date)

    # ---- payload --------------------------------------------------------

    def window_start(self) -> datetime:
        s = self.settings()
        if s["last_sync_day"]:
            d = datetime.strptime(s["last_sync_day"], "%Y-%m-%d") - timedelta(days=OVERLAP_DAYS)
        elif s["first_sync_start"]:
            d = datetime.strptime(s["first_sync_start"], "%Y-%m-%d")
        else:
            d = datetime.now(ET).replace(tzinfo=None)
        return d.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=ET)

    def _trades(self, start: datetime):
        """Edge Trader round trips touching the window, built from every stored fill (so a trade
        opened before the window keeps its first fill and its annotation id)."""
        txns = self.store.transactions_since((start - timedelta(days=60)).astimezone(timezone.utc)
                                             .strftime("%Y-%m-%dT%H:%M:%S"))
        fills = fills_from_transactions(txns)
        by_id = {f.id: f for f in fills}
        book = build_book(fills)
        # A fill is in the window by its time or by the trading day Schwab books it to: an evening
        # futures fill (after 18:00 ET) belongs to the next day's session, as "Closed today" shows it.
        day0 = start.date()
        def inside(x):
            day = _session_day(x)
            return (_parse(x.get("time")) or start) >= start or (day is not None and day >= day0)
        window_txns = [x for x in txns if inside(x)]
        in_window = {str(x.get("activityId")) for x in window_txns}
        out = []
        for t in book.trades:
            fl = [by_id[i] for i in t.fills if i in by_id]
            if fl and (max(f.time for f in fl) >= start or any(aid(f.id) in in_window for f in fl)):
                out.append((t, fl))
        return out, window_txns

    def _orders_by_symbol(self) -> dict[str, list[dict]]:
        nodes: dict[str, list[dict]] = {}

        def walk(o):
            for leg in o.get("orderLegCollection") or []:
                sym = (leg.get("instrument") or {}).get("symbol")
                if sym:
                    nodes.setdefault(sym, []).append({**o, "_instruction": leg.get("instruction"),
                                                      "_qty": leg.get("quantity")})
                    break
            for c in o.get("childOrderStrategies") or []:
                walk(c)
        for o in self.store.all_orders():
            walk(o)
        return nodes

    def _tickets(self, start: datetime) -> dict[int, dict]:
        return {t["order_id"]: t for t in self.store.tickets_since((start - timedelta(days=60)).isoformat())
                if t.get("order_id") and t.get("status") == "ACCEPTED"}

    def _exits(self, trade, fills, orders: list[dict], taken: set):
        """Stop and target orders on the trade's exit side, entered while it was open. A trade whose
        entry predates the fill history starts at its first known fill. Each order belongs to one
        trade only (`taken`): trades are visited newest first, so an order entered after a re-entry
        belongs to the re-entry."""
        exit_side = EXIT_LONG if trade.side == "LONG" else EXIT_SHORT
        lo = (trade.opened or min(f.time for f in fills)) - timedelta(seconds=5)
        # The entry's own OCO children carry the entry's enteredTime, which can be long before the fill.
        entry_tree: set = set()
        first = min(fills, key=lambda f: f.time)
        stack = [self.store.get_order(first.order_id)] if first.order_id else []
        while stack:
            n = stack.pop()
            if n:
                entry_tree.add(n.get("orderId"))
                stack.extend(n.get("childOrderStrategies") or [])
        hi = trade.closed + timedelta(seconds=1) if trade.closed else None
        out = []
        for o in orders:
            if o.get("_instruction") not in exit_side or o.get("orderId") in taken:
                continue
            ent = _parse(o.get("enteredTime"))
            if not ent or (ent < lo and o.get("orderId") not in entry_tree) or (hi and ent > hi):
                continue
            taken.add(o.get("orderId"))
            ot = o.get("orderType") or ""
            if ot in STOP_TYPES:
                role = "stop"
            elif ot == "LIMIT":
                role = "target"
            else:
                continue
            out.append((ent, role, o))
        out.sort(key=lambda x: (x[0], x[2].get("orderId") or 0))
        return out

    def _events_and_facts(self, trade, fills, exits, tickets):
        events, facts = [], {}
        entry = trade.avg_entry
        seen = {"stop": None, "target": None}
        first_stop = None
        for ent, role, o in exits:
            oid = o.get("orderId")
            price = o.get("stopPrice") if role == "stop" else o.get("price")
            trailing = (o.get("orderType") or "").startswith("TRAILING")
            if role == "stop" and first_stop is None and price is not None and not trailing:
                first_stop = float(price)
            if trailing:
                kind = "trail"
                text = f"trailing stop {o.get('stopPriceOffset') or ''} {o.get('stopPriceLinkType') or ''}".strip()
            elif role == "stop" and seen["stop"] is not None:
                kind = "breakeven" if entry and price is not None and abs(float(price) - entry) < 1e-6 else "stop_moved"
                text = None
            else:
                kind = f"{role}_set" if seen[role] is None else f"{role}_moved"
                text = None
            seen[role] = price
            events.append({"id": f"o{oid}:set", "ts": ent.isoformat(), "kind": kind,
                           "price": float(price) if price is not None else None,
                           "qty": float(o.get("_qty") or o.get("quantity") or 0) or None,
                           "text": text, "origin": "schwab"})
            if o.get("status") == "CANCELED" and o.get("closeTime"):
                events.append({"id": f"o{oid}:cancel", "ts": _parse(o["closeTime"]).isoformat(),
                               "kind": f"{role}_cancelled", "price": float(price) if price is not None else None,
                               "qty": None, "text": None, "origin": "schwab"})
        events = _collapse(events)
        # Facts: the ticket that opened the trade wins; else the first fixed stop Schwab shows.
        ticket = next((tickets[f.order_id] for f in fills if f.order_id in tickets), None)
        stop = None
        if ticket:
            for v in (ticket.get("stop"), self.store.frozen_stop(ticket["ticket_id"])):
                try:
                    stop = float(v)
                    break
                except (TypeError, ValueError):
                    continue
            try:
                facts["target_price"] = float(str(ticket.get("target") or "").split(",")[0])
            except ValueError:
                pass
            if ticket.get("risk"):
                facts["risk_per_trade"] = round(float(ticket["risk"]), 2)
        if stop is None:
            stop = first_stop
        if stop is not None and entry:
            rps = abs(entry - stop)
            if rps > 0 and ((trade.side == "LONG" and stop < entry) or (trade.side == "SHORT" and stop > entry)):
                facts["stop_loss"] = stop
                risk = rps * trade.max_qty * (trade.multiplier or 1)
                facts.setdefault("risk_per_trade", round(risk, 2))
                if facts.get("target_price"):
                    facts["risk_reward"] = round(abs(facts["target_price"] - entry) / rps, 2)
                if trade.closed and trade.net is not None and facts["risk_per_trade"]:
                    facts["r_multiple"] = round(trade.net / facts["risk_per_trade"], 2)
        return events, facts

    def _spread_orders(self) -> dict:
        """Order id -> the companion spread that sent it ({spread_id, kind, width}). Empty when this
        build has no spreads ledger."""
        try:
            spreads = self._rows("select s.spread_id, s.kind, s.entry_order_id, s.close_order_id, s.target_order_id, "
                                 "t.width from spreads s left join spread_tickets t on t.spread_id = s.spread_id")
            fills = self._rows("select spread_id, order_id from spread_fills")
        except sqlite3.OperationalError:
            return {}
        info = {r["spread_id"]: {"spread_id": r["spread_id"], "kind": r["kind"] or "", "width": r["width"],
                                 "orders": set()} for r in spreads}
        out = {}
        for r in spreads:
            for o in (r["entry_order_id"], r["close_order_id"], r["target_order_id"]):
                if o:
                    out[int(o)] = info[r["spread_id"]]
        for r in fills:
            if r["order_id"] and r["spread_id"] in info:
                out[int(r["order_id"])] = info[r["spread_id"]]
        for o, sp in out.items():
            sp["orders"].add(o)
        return out

    def _units(self, trades):
        """[(unit, spread)]: each trade alone (spread None), except the leg round trips of one
        companion spread, which go together (in the order they come)."""
        of_order = self._spread_orders()
        units, by_spread = [], {}
        for t, fl in trades:
            sp = next((of_order[int(f.order_id)] for f in fl if f.order_id and int(f.order_id) in of_order), None)
            if sp is None:
                units.append(([(t, fl)], None))
            elif sp["spread_id"] in by_spread:
                by_spread[sp["spread_id"]][0].append((t, fl))
            else:
                by_spread[sp["spread_id"]] = ([(t, fl)], sp)
                units.append(by_spread[sp["spread_id"]])
        return units

    def _spread_events(self, sp: dict) -> list[dict]:
        """A companion spread's timeline: its GTC target orders (set, moved, cancelled), its close
        orders (filled, or cancelled to move), and its app-watched stop (set, moved, removed, hit,
        sent). Spread orders are NET_CREDIT / NET_DEBIT and the stop lives in the app, so the
        single-leg exit reader never sees them."""
        sid = sp["spread_id"]
        try:
            roles = self._rows("select order_id, max(role) as role from spread_fills where spread_id = ? "
                               "group by order_id", (sid,))
            stops = self._rows("select created, kind, mid, detail from spread_stop_events where spread_id = ? "
                               "order by rowid", (sid,))
        except sqlite3.OperationalError:
            return []
        events = []
        for r in roles:
            o = self.store.get_order(r["order_id"]) if r["order_id"] else None
            if not o or r["role"] not in ("target", "close"):
                continue
            oid, ent = o.get("orderId"), _parse(o.get("enteredTime"))
            price = o.get("price")
            price = float(price) if price is not None else None
            qty = max((float(l.get("quantity") or 0) for l in o.get("orderLegCollection") or []), default=0) or None
            done = _parse(o.get("closeTime"))
            filled = float(o.get("filledQuantity") or 0)
            net = _net_fill(o)
            if r["role"] == "target":
                if ent:
                    events.append({"id": f"s{oid}:set", "ts": ent.isoformat(), "kind": "target_set", "price": price,
                                   "qty": qty, "text": "spread target (GTC net limit)", "origin": "schwab"})
                if o.get("status") == "CANCELED" and done and filled < 1e-9:
                    events.append({"id": f"s{oid}:cancel", "ts": done.isoformat(), "kind": "target_cancelled",
                                   "price": price, "qty": None, "text": None, "origin": "schwab"})
            if filled > 1e-9 and done and o.get("status") in ("FILLED", "CANCELED", "EXPIRED", "REPLACED"):
                what = "spread target filled" if r["role"] == "target" else "spread close filled"
                if o.get("status") != "FILLED":
                    what += f", {filled:g} of {qty or 0:g}, rest {o['status'].lower()}"
                events.append({"id": f"s{oid}:close", "ts": done.isoformat(), "kind": "close",
                               "price": net if net is not None else price, "qty": filled,
                               "text": what + ("" if net is not None else " (limit price; fill price not listed)"),
                               "origin": "schwab"})
            elif r["role"] == "close" and o.get("status") in ("CANCELED", "EXPIRED") and done and filled < 1e-9:
                events.append({"id": f"s{oid}:cancel", "ts": done.isoformat(), "kind": "other", "price": price,
                               "qty": qty, "text": f"spread close at {price} cancelled, nothing filled" if price is not None
                               else "spread close cancelled, nothing filled", "origin": "schwab"})
        have_stop = False
        for e in stops:
            ts = _parse(e["created"])
            if not ts:
                continue
            m = re.search(r"stop ([0-9.]+)", e["detail"] or "")
            price = float(m.group(1)) if m else None
            send = "send when hit" in (e["detail"] or "")
            base = {"id": f"ss:{sid}:{e['created']}:{e['kind']}"[:100], "ts": ts.isoformat(), "price": None,
                    "qty": None, "origin": "edge"}
            if e["kind"] == "set":
                events.append({**base, "kind": "stop_moved" if have_stop else "stop_set", "price": price,
                               "text": "app stop (no order at Schwab), " + ("sends a close when hit" if send else "alert only")})
                have_stop = True
            elif e["kind"] == "cleared":
                events.append({**base, "kind": "stop_cancelled", "text": "app stop removed"})
                have_stop = False
            elif e["kind"] == "would_fire":
                events.append({**base, "kind": "other", "price": e["mid"],
                               "text": f"app stop hit (mid {e['mid']})" if e["mid"] is not None else "app stop hit"})
            elif e["kind"] == "sent":
                events.append({**base, "kind": "other", "text": f"app stop sent: {e['detail']}"[:500]})
            elif e["kind"] == "dismissed":
                events.append({**base, "kind": "other", "text": "app stop alert dismissed"})
        events.sort(key=lambda e: (e["ts"], e["id"]))
        had = False                       # a later target order is a move of the spread's target
        for e in events:
            if e["kind"] == "target_set":
                e["kind"] = "target_moved" if had else "target_set"
                had = True
        # Only the target's cancel-and-replace noise collapses; app stop events stay as they happened
        # (a switch from alert only to send at the same price is a real change).
        targets = _collapse([e for e in events if ROLE.get(e["kind"]) == "target"])
        rest = [e for e in events if ROLE.get(e["kind"]) != "target"]
        return sorted(targets + rest, key=lambda e: (e["ts"], e["id"]))

    @staticmethod
    def _spread_facts(unit, sp: dict, own: list) -> dict:
        """A vertical's risk from its own fills: max loss (credit: width minus the net credit; debit: the
        net debit) x contracts x multiplier, and its R multiple once both legs closed. Nothing (the
        journal gets no spread numbers) unless it is a clean two-leg spread the journal books as ONE
        trade: both round trips made only of the spread's own fills, the same size and multiplier on
        both legs, and every spread order's fills paired the way the journal pairs a vertical (two
        option fills of one order in one second, the same quantity)."""
        legs = [t for t, _fl in unit]
        short = next((t for t in legs if t.side == "SHORT"), None)
        long_ = next((t for t in legs if t.side == "LONG"), None)
        try:
            width = float(sp.get("width") or 0)
        except (TypeError, ValueError):
            width = 0.0
        if len(legs) != 2 or not short or not long_ or not width or not short.avg_entry or not long_.avg_entry:
            return {}
        if any(f.order_id is None or int(f.order_id) not in sp["orders"] for _t, fl in unit for f in fl):
            return {}                    # a leg's round trip has fills that are not the spread's
        if abs(short.max_qty - long_.max_qty) > 1e-9 or (short.multiplier or 0) != (long_.multiplier or 0):
            return {}
        by_sec: dict = {}
        for f in own:
            by_sec.setdefault((int(f.order_id), f.time.replace(microsecond=0)), []).append(f)
        for fs in by_sec.values():
            if len(fs) != 2 or fs[0].symbol == fs[1].symbol or abs(abs(fs[0].qty) - abs(fs[1].qty)) > 1e-9:
                return {}                # the journal keeps these as separate legs: no whole-spread numbers
        net = abs(short.avg_entry - long_.avg_entry)
        per = width - net if sp["kind"].endswith("CREDIT") else net
        risk = round(per * short.max_qty * (short.multiplier or 100), 2)
        if risk <= 0:
            return {}
        facts = {"risk_per_trade": risk}
        if short.closed and long_.closed and short.net is not None and long_.net is not None:
            facts["r_multiple"] = round((short.net + long_.net) / risk, 2)
        return facts

    def build_payload(self) -> tuple[dict, dict]:
        s = self.settings()
        if not self.configured():
            raise JournalError("Set the journal address and pick its accounts first (Journal settings).")
        start = self.window_start()
        built_at = now_iso()
        trades, window_txns = self._trades(start)
        earliest = (self._row("select min(time) as t from transactions") or {}).get("t")
        orders = self._orders_by_symbol()
        tickets = self._tickets(start)
        annotations, events = [], []
        stored = {r["id"]: r for r in self._rows("select * from journal_annotations")}
        notes = self._rows("select * from journal_notes")
        by_key = {}
        for r in sorted(stored.values(), key=lambda r: r["created"] or ""):
            for k in json.loads(r["fill_keys"] or "[]"):
                by_key.setdefault(k, r["id"])
        excluded = {str(x).strip().upper() for x in s["exclude_symbols"] if str(x).strip()}
        # Activity ids of excluded fills: a trade with any of them sends nothing (no entry, events or notes).
        held_back = {str(x.get("activityId")) for x in window_txns
                     if x.get("type") == "TRADE" and _excluded(x, excluded)}
        taken: set = set()
        exits = {}
        for t, fl in sorted(trades, key=lambda x: x[0].opened or min(f.time for f in x[1]), reverse=True):
            exits[id(t)] = self._exits(t, fl, orders.get(t.symbol, []), taken)
        for unit, sp in self._units(trades):
            # One unit is one trade, or a companion spread's leg round trips (the journal books a
            # vertical as one trade): one annotation, keyed on the spread's first fill.
            all_fills = sorted((f for _t, fl in unit for f in fl), key=lambda f: (f.time, f.id))
            if sp is not None:          # only the spread's own fills: an add made in thinkorswim is not the spread
                all_fills = [f for f in all_fills if f.order_id and int(f.order_id) in sp["orders"]]
            fill_ids = [f.id for f in all_fills] or [i for t, _fl in unit for i in t.fills]
            if any(aid(i) in held_back for _t, fl in unit for i in [x.id for x in fl] + list(_t.fills)):
                for k in {aid(f) for f in fill_ids}:
                    stored.pop(by_key.get(k), None)        # nor through the outside-window path below
                continue
            ann_id = self.annotation_id(fill_ids)
            if ann_id not in stored:
                ann_id = next((by_key[aid(f)] for f in fill_ids if aid(f) in by_key), ann_id)
            if sp is None:
                t, fl = unit[0]
                evs, facts = self._events_and_facts(t, fl, exits[id(t)], tickets)
            else:
                evs, facts = self._spread_events(sp), self._spread_facts(unit, sp, all_fills)
            row = stored.pop(ann_id, None)
            fields = {**facts, **(json.loads(row["fields"]) if row and row["fields"] else {})}
            tags = json.loads(row["tags"]) if row and row["tags"] else None
            keys = sorted({aid(f) for f in fill_ids} | set(json.loads(row["fill_keys"]) if row else []))
            ann_notes = [n for n in notes if n["annotation_id"] == ann_id]
            if not (fields or tags or evs or ann_notes):
                continue
            annotations.append({"id": ann_id, "fill_keys": keys, "fields": fields, "tags": tags,
                                "updated_at": (row or {}).get("updated")})
            for e in evs:
                events.append({**e, "annotation_id": ann_id})
            for n in ann_notes:
                events.append({"id": n["id"], "annotation_id": ann_id, "ts": n["ts"], "kind": "note",
                               "price": None, "qty": None, "text": n["text"], "origin": "edge"})
        # Entries made on trades now outside the window (edited after an earlier sync).
        last = s["last_sync_at"] or ""
        for ann_id, row in stored.items():
            if (row["updated"] or "") < last or self._keys_excluded(json.loads(row["fill_keys"] or "[]"), excluded):
                continue
            annotations.append({"id": ann_id, "fill_keys": json.loads(row["fill_keys"]),
                                "fields": json.loads(row["fields"] or "{}"),
                                "tags": json.loads(row["tags"]) if row["tags"] else None,
                                "updated_at": row["updated"]})
            for n in notes:
                if n["annotation_id"] == ann_id:
                    events.append({"id": n["id"], "annotation_id": ann_id, "ts": n["ts"], "kind": "note",
                                   "price": None, "qty": None, "text": n["text"], "origin": "edge"})
        day_account = s["routes"].get("STOCK") or next(iter(s["routes"].values()))
        day_notes = [{"date": r["date"], "account_id": day_account, "text": r["text"], "updated_at": r["updated"]}
                     for r in self._rows("select * from journal_day_notes where date >= ? or updated >= ?",
                                         (start.date().isoformat(), last))]
        # A pick covers the whole round trip: a fill that joins the trade later goes to the same account.
        picked = self.overrides()
        overrides = {}
        for t, _fl in trades:
            ids = sorted({aid(f) for f in t.fills})
            acct = next((picked[i] for i in ids if i in picked), None)
            if acct is not None:
                overrides.update({i: acct for i in ids})
        n_excluded = sum(len(_legs(x)) or 1 for x in window_txns if x.get("type") == "TRADE" and _excluded(x, excluded))
        sent_ids = {str(x.get("activityId")) for x in window_txns
                    if x.get("type") == "TRADE" and not _excluded(x, excluded)}
        overrides = {k: v for k, v in overrides.items() if k in sent_ids}
        payload = {"routes": s["routes"], "dedup_accounts": sorted(set(s["dedup_accounts"]) | set(s["routes"].values())),
                   "account_overrides": overrides,
                   "exclude_symbols": s["exclude_symbols"],
                   "transactions": [_for_journal(x) for x in window_txns
                                    if x.get("type") == "TRADE" and not _excluded(x, excluded)],
                   "annotations": annotations, "events": events, "day_notes": day_notes,
                   "deleted_events": [r["id"] for r in self._rows("select id from journal_tombstones")]}
        meta = {"window_start": start.isoformat(), "history_starts": earliest,
                "history_short": bool(earliest and start.astimezone(timezone.utc).isoformat() < earliest[:19]),
                "trades": len(trades), "built_at": built_at, "excluded_here": n_excluded}
        return payload, meta

    def _keys_excluded(self, keys: list[str], excluded: set[str]) -> bool:
        """True when any of these journal fill keys (activity ids) is a fill of an excluded symbol."""
        ids = [int(k) for k in keys if str(k).isdigit()]
        if not ids or not excluded:
            return False
        rows = self._rows(f"select payload from transactions where activity_id in ({','.join('?' * len(ids))})", ids)
        return any(_excluded(json.loads(r["payload"]), excluded) for r in rows)

    # ---- preview / apply ------------------------------------------------

    def _log(self, dry_run: bool, ok: bool, window: str | None, summary=None, error=None) -> None:
        self._write("insert into journal_sync_log (at, dry_run, ok, window_start, summary, error) values (?,?,?,?,?,?)",
                    (now_iso(), int(dry_run), int(ok), window, json.dumps(summary)[:20000] if summary else None,
                     (error or "")[:2000] or None))

    def preview(self) -> dict:
        if self.demo:
            raise JournalError("Demo mode: no journal sync.")
        with self._lock:
            payload, meta = self.build_payload()
            code, res = self._http("POST", "/api/sync/edge-trader", {**payload, "dry_run": True})
            if code != 200 or not isinstance(res, dict):
                msg = (res or {}).get("detail") if isinstance(res, dict) else None
                self._log(True, False, meta["window_start"], error=f"HTTP {code}: {msg}")
                raise JournalError(f"The journal refused the preview (HTTP {code}): {msg or 'no detail'}")
            self._pending = {"token": res["state_token"], "payload": payload, "at": time.time(),
                             "built_at": meta["built_at"], "meta": meta}
            self._log(True, True, meta["window_start"], summary={k: res.get(k) for k in ("fills", "events")})
            if isinstance(res.get("fills"), dict) and meta.get("excluded_here"):
                try:
                    res["fills"]["excluded"] = int(res["fills"].get("excluded") or 0) + meta["excluded_here"]
                except (TypeError, ValueError):
                    res["fills"]["excluded"] = meta["excluded_here"]
            return {**res, "meta": meta}

    def apply(self, token: str) -> dict:
        if self.demo:
            raise JournalError("Demo mode: no journal sync.")
        with self._lock:
            p = self._pending
            if not p or p["token"] != token or time.time() - p["at"] > PREVIEW_TTL:
                raise JournalError("This preview is out of date. Press Sync again.")
            code, res = self._http("POST", "/api/sync/edge-trader",
                                   {**p["payload"], "dry_run": False, "state_token": token}, timeout=APPLY_TIMEOUT)
            if code == 409 and isinstance(res, dict):
                self._pending = {**p, "token": res["state_token"], "at": time.time()}
                self._log(False, False, None, error="journal changed since the preview")
                return {**res, "conflict": True, "meta": p.get("meta")}
            if code != 200 or not isinstance(res, dict) or not res.get("applied"):
                msg = (res or {}).get("detail") if isinstance(res, dict) else None
                self._log(False, False, None, error=f"HTTP {code}: {msg}")
                raise JournalError(f"The journal did not apply the sync (HTTP {code}): {msg or 'no detail'}. "
                                   "Nothing is half-written: the journal applies all or nothing.")
            self._pending = None
            s = self.settings()
            s["last_sync_day"] = datetime.now(ET).date().isoformat()
            s["last_sync_at"] = p["built_at"]      # anything edited after the payload was built goes next time
            self.store.set_setting("journal", json.dumps(s))
            sent = p["payload"].get("deleted_events") or []
            if sent:
                self._write("delete from journal_tombstones where id in (%s)" % ",".join("?" * len(sent)), sent)
            self._log(False, True, None, summary={k: res.get(k) for k in ("fills", "events", "backup")})
            return res

    def status(self) -> dict:
        s = self.settings()
        last = self._row("select at, dry_run, ok, error from journal_sync_log where dry_run=0 order by id desc limit 1")
        return {"configured": self.configured(), "url": s["url"], "last_sync_at": s["last_sync_at"],
                "last_sync_day": s["last_sync_day"], "last_apply": last, "routes": s["routes"]}
