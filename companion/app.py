"""FastAPI server: serves the dashboard, the cached state, and the order ticket.

    python -m companion            (http://127.0.0.1:8770)

GET endpoints never call Schwab per request (cached state), except the ticket's plan quote.
POST endpoints (ticket) require the X-Companion header and a same-origin request, so another
web page open in the browser cannot place orders through this server.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import __version__
from .poller import Poller
from .trading import Trader

STATIC = Path(__file__).resolve().parent / "static"
PORT = int(os.environ.get("COMPANION_PORT", "8770"))
ORIGINS = {f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}"}
DEMO = os.environ.get("EDGE_TRADER_DEMO") == "1"     # synthetic data, no Schwab, no orders


class DemoPoller:
    """Stands in for the poller in demo mode: synthetic state, never touches Schwab."""

    def __init__(self):
        from .store import Store
        self.store = Store(Path(__file__).resolve().parent.parent / "data" / "demo.db")
        self.terminal, self.orders_today, self.trader = set(), {}, None

    def snapshot(self) -> dict:
        from .demo import demo_state
        st = demo_state()
        if self.trader is not None:
            st["trading"] = {**self.trader.status(), "market_open": True}
        return st

    def start(self): pass
    def stop(self): pass
    def _merged_orders(self): return []
    def note_external_request(self, retry_after=None): pass
    def cooldown_left(self): return 0.0


poller = DemoPoller() if DEMO else Poller()
trader = Trader(poller.store)
poller.trader = trader
trader.on_request = poller.note_external_request
trader.cooldown_left = poller.cooldown_left


@asynccontextmanager
async def lifespan(_app: FastAPI):
    trader.arm(False, reason="startup")   # arming never survives a restart
    poller.start()
    if not DEMO:
        import threading
        from . import update_check
        threading.Thread(target=lambda: UPDATE.update({"info": update_check.check()}), daemon=True).start()
    yield
    poller.stop()


app = FastAPI(title="Edge Trader for Schwab", lifespan=lifespan, docs_url=None, redoc_url=None)
HOSTS = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}


@app.middleware("http")
async def only_local_host(request: Request, call_next):
    """A page on another domain that resolves to 127.0.0.1 (DNS rebinding) sends its own Host
    header: refuse it, so no other site can read positions or balances."""
    if request.headers.get("host") not in HOSTS:
        return JSONResponse({"error": "unexpected host"}, status_code=403)
    if DEMO and request.method == "POST" and (request.url.path.startswith("/api/position/")
                                              or request.url.path in ("/api/ticket/submit", "/api/ticket/cancel",
                                                                      "/api/ticket/cancel_entries", "/api/ticket/arm",
                                                                      "/api/ticket/preview", "/api/ticket/plan",
                                                                      "/api/ticket/lookup")):
        return JSONResponse({"ok": False, "error": "Demo mode: no orders and no Schwab calls."})
    return await call_next(request)


def _guard(request: Request) -> None:
    if request.headers.get("x-companion") != "1":
        raise HTTPException(403, "missing X-Companion header")
    origin = request.headers.get("origin")
    if origin and origin not in ORIGINS:
        raise HTTPException(403, "cross-origin request refused")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-store"})


ICON = Path(__file__).resolve().parent.parent / "assets" / "companion.ico"


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    """Same candlestick icon as the desktop shortcut, so the tab is easy to spot."""
    return FileResponse(ICON, media_type="image/x-icon", headers={"Cache-Control": "max-age=86400"})


@app.get("/icon.png", include_in_schema=False)
def icon_png():
    return FileResponse(ICON.with_name("companion-64.png"), media_type="image/png",
                        headers={"Cache-Control": "max-age=86400"})


# Shared by the main page and the floating windows (they run in different browsers, so
# localStorage is not shared): the selected position and the UI preferences (hotkeys, partial mode).
UI = {"sel": None, "sel_seq": 0, "prefs_rev": 0}
UPDATE: dict = {"info": None}        # a newer release on GitHub, if any (notify only)


@app.get("/api/state")
def state():
    return JSONResponse({**poller.snapshot(), "ui": UI, "update": UPDATE["info"], "demo": DEMO,
                         "version": __version__}, headers={"Cache-Control": "no-store"})


@app.post("/api/ui/select")
async def ui_select(request: Request):
    _guard(request)
    sym = (await request.json()).get("symbol")
    UI["sel"] = str(sym)[:40] if sym else None
    UI["sel_seq"] += 1
    return UI


@app.get("/api/ui/prefs")
def ui_prefs_get():
    try:
        return json.loads(trader.store.get_setting("ui_prefs") or "{}")
    except ValueError:
        return {}


@app.post("/api/ui/prefs")
async def ui_prefs_set(request: Request):
    _guard(request)
    body = await request.json()
    prefs = {k: body[k] for k in ("hotkeys", "partMode") if k in body}
    if "riskChoices" in body:
        try:
            vals = [round(float(v), 2) for v in body["riskChoices"]][:4]
        except (TypeError, ValueError):
            raise HTTPException(400, "risk amounts must be numbers")
        if len(vals) != 4 or any(not (0 < v <= 100000) for v in vals):
            raise HTTPException(400, "four risk amounts above $0")
        prefs["riskChoices"] = vals
    cur = ui_prefs_get()
    cur.update(prefs)
    trader.store.set_setting("ui_prefs", json.dumps(cur))
    UI["prefs_rev"] += 1
    return cur


@app.post("/api/floating/open")
async def floating_open(request: Request):
    """Open a floating Entry or Manage window (a separate windowless process)."""
    _guard(request)
    kind = (await request.json()).get("kind")
    if kind not in ("entry", "manage"):
        raise HTTPException(400, "kind must be entry or manage")
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    subprocess.Popen([str(pyw if pyw.exists() else exe), "-m", "companion.floating", kind],
                     cwd=str(Path(__file__).resolve().parent.parent))
    return {"ok": True}


@app.post("/api/ticket/plan")
async def ticket_plan(request: Request):
    _guard(request)
    try:
        req = await request.json()
        out = (await run_in_threadpool(trader.make_plan, req)).as_dict()
        q = trader.quote(req["symbol"], max_age=5.0)   # cache hit from make_plan: no extra call
        out.update({"name": q.get("name"), "last": q.get("last"), "change_pct": q.get("change_pct")})
        return out
    except Exception as e:
        return JSONResponse({"ok": False, "errors": [f"{e}"[:300]]}, status_code=200)


@app.post("/api/ticket/lookup")
async def ticket_lookup(request: Request):
    """Company name and last price, so you can confirm the symbol before anything else."""
    _guard(request)
    sym = str((await request.json()).get("symbol") or "").strip().upper()
    if not sym or len(sym) > 10:
        return {"found": False, "symbol": sym}
    try:
        return await run_in_threadpool(trader.quote, sym, 2.0)
    except Exception as e:
        return {"found": False, "symbol": sym, "error": f"{e}"[:200]}


@app.post("/api/ticket/preview")
async def ticket_preview(request: Request):
    _guard(request)
    return await run_in_threadpool(trader.preview, await request.json())


# Order actions call Schwab and wait on it; they run in worker threads so the dashboard
# (/api/state) keeps updating while a close or cancel is in flight.

@app.post("/api/ticket/submit")
async def ticket_submit(request: Request):
    _guard(request)
    return await run_in_threadpool(trader.submit, await request.json())


@app.post("/api/ticket/cancel")
async def ticket_cancel(request: Request):
    """Cancel one unfilled entry. No arming needed: cancelling an entry only removes risk."""
    _guard(request)
    return await run_in_threadpool(trader.cancel_entry, int((await request.json()).get("order_id")))


@app.post("/api/ticket/cancel_entries")
async def ticket_cancel_entries(request: Request):
    _guard(request)
    tops = [o for o in poller._merged_orders() if o.get("orderId") not in poller.terminal]
    return await run_in_threadpool(trader.cancel_all_entries, tops)


@app.post("/api/position/cancel_orders")
async def position_cancel_orders(request: Request):
    """Cancel every working order for one symbol (entries, stops, targets); the position stays."""
    _guard(request)
    sym = str((await request.json()).get("symbol") or "")
    return await run_in_threadpool(trader.cancel_symbol_orders, sym, poller._merged_orders(),
                                   set(poller.terminal))


@app.post("/api/position/close")
async def position_close(request: Request):
    """One market order for the whole position; Schwab cancels its stop/target as it enters."""
    _guard(request)
    sym = str((await request.json()).get("symbol") or "")
    return await run_in_threadpool(trader.close_position, sym, poller._merged_orders(),
                                   set(poller.terminal))


@app.post("/api/position/move_stop")
async def position_move_stop(request: Request):
    """Move the position's stop(s) to a price, or to break-even with {"be": true}."""
    _guard(request)
    body = await request.json()
    sym = str(body.get("symbol") or "").upper().strip()
    price = body.get("stop")
    if body.get("be"):
        pos = next((p for p in (poller.snapshot().get("positions") or []) if p["symbol"] == sym), None)
        price = ((pos.get("r") or {}).get("entry") or pos.get("avg_price")) if pos else None
        if not price:
            return {"ok": False, "symbol": sym, "error": "No entry price known for break-even."}
    try:
        float(price)
    except (TypeError, ValueError):
        return {"ok": False, "symbol": sym, "error": "Enter a stop price."}
    return await run_in_threadpool(trader.move_stop, sym, price, poller._merged_orders(), set(poller.terminal))


@app.post("/api/position/partial")
async def position_partial(request: Request):
    """Take part of the position (pct 0-1 or a share count), mid or market, then rebuild the
    stop and target on the rest."""
    _guard(request)
    body = await request.json()
    sym = str(body.get("symbol") or "").upper().strip()
    pos = next((p for p in (poller.snapshot().get("positions") or []) if p["symbol"] == sym), None)
    prior = ((pos or {}).get("trail") or {}).get("level")
    return await run_in_threadpool(trader.take_partial, sym, body.get("pct"), body.get("shares"),
                                   poller._merged_orders(), set(poller.terminal), body.get("mode") or "mid", prior)


@app.post("/api/position/set_target")
async def position_set_target(request: Request):
    """One target for the whole position at {"price": x} or {"r": n} (frozen R), paired with
    the current stop in one OCO."""
    _guard(request)
    body = await request.json()
    sym = str(body.get("symbol") or "").upper().strip()
    r = body.get("r")
    # R only from the ticket that opened THIS position (the row's Live R basis), never an older one.
    pos = next((p for p in (poller.snapshot().get("positions") or []) if p["symbol"] == sym), None)
    return await run_in_threadpool(trader.set_target, sym, body.get("price"), float(r) if r else None,
                                   poller._merged_orders(), set(poller.terminal), pos and pos.get("r"),
                                   ((pos or {}).get("trail") or {}).get("level"))


@app.post("/api/position/trail")
async def position_trail(request: Request):
    """Replace the exits with the kept target + a Schwab trailing stop: {"unit": "R"|"$"|"%",
    "value": n, "basis": "MARK"}. R uses the frozen basis of the ticket that opened this position."""
    _guard(request)
    body = await request.json()
    sym = str(body.get("symbol") or "").upper().strip()
    pos = next((p for p in (poller.snapshot().get("positions") or []) if p["symbol"] == sym), None)
    return await run_in_threadpool(trader.set_trail, sym, body.get("unit") or "R", body.get("value"),
                                   body.get("basis") or "MARK", poller._merged_orders(), set(poller.terminal),
                                   pos and pos.get("r"), ((pos or {}).get("trail") or {}).get("level"))


@app.post("/api/position/resolve_close")
async def position_resolve_close(request: Request):
    _guard(request)
    trader.resolve_close(str((await request.json()).get("symbol") or ""))
    return {"ok": True}


@app.post("/api/ticket/arm")
async def ticket_arm(request: Request):
    _guard(request)
    trader.arm(bool((await request.json()).get("armed")), reason="dashboard click")
    return trader.status()


@app.post("/api/ticket/resolve")
async def ticket_resolve(request: Request):
    _guard(request)
    trader.resolve(str((await request.json()).get("ticket_id")))
    return trader.status()


LOG = Path(__file__).resolve().parent.parent / "data" / "companion.log"


def main() -> None:
    import sys
    import uvicorn
    # The launcher runs us windowless (pythonw: no console to close by accident), so all output,
    # including any crash traceback, goes to data/companion.log.
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = open(LOG, "a", encoding="utf-8", buffering=1)
        handlers = [logging.StreamHandler(sys.stderr)]               # already the log file
    else:
        handlers = [logging.FileHandler(LOG, encoding="utf-8"), logging.StreamHandler(sys.stderr)]
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("companion").info("Edge Trader %s starting (pid %s)%s", __version__, os.getpid(),
                                        " in DEMO mode" if DEMO else "")
    # One process, no reload: exactly one poller.
    uvicorn.run(app, host="127.0.0.1", port=PORT, workers=1, log_level="warning")
