"""Spread routes and page script, registered by app.py only when this package is present."""
from __future__ import annotations

import threading
from pathlib import Path

from fastapi import Request
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from .spread_desk import SpreadDesk

STATIC = Path(__file__).resolve().parent / "static"
DEMO_REFUSAL = {"ok": False, "error": "Demo mode: no orders and no Schwab calls."}


def register(app, trader, poller, guard, demo: bool = False) -> list[str]:
    """Add the spread ticket's endpoints; returns the page scripts the dashboard should load."""
    if not trader.spreads:
        return []
    desk = SpreadDesk(trader)
    desk.snapshot = poller._merged_orders
    desk.sweep_fresh = (lambda: poller._fresh("orders_sweep")) if hasattr(poller, "_fresh") else None

    def held_quote(symbol: str):
        """The poller's latest quote of a contract it already polls (every held leg), if current."""
        if not hasattr(poller, "_fresh") or not poller._fresh("quotes"):
            return None
        e = (getattr(poller, "quotes", None) or {}).get(symbol) or {}
        q = e.get("quote") or {}
        if q.get("bidPrice") is None or q.get("askPrice") is None:
            return None
        return {"symbol": symbol, "found": True, "bid": q.get("bidPrice"), "ask": q.get("askPrice"),
                "mark": q.get("mark"), "realtime": e.get("realtime"),
                "is_penny": ((e.get("reference") or {}).get("isPennyPilot"))}
    desk.held_quotes = held_quote

    def fire(sid: str) -> None:
        """A stop with Send on fired: its one close goes out on a worker thread (the poller never waits)."""
        if not demo:
            threading.Thread(target=desk.auto_stop, args=(sid,), name=f"auto-stop-{sid}", daemon=True).start()
    trader.spreads.on_fire = fire

    @app.get("/features/{name}.js", include_in_schema=False)
    def feature_js(name: str):
        f = STATIC / f"{name}.js"
        if name not in ("spreads",) or not f.exists():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(f, media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.post("/api/spread/plan")
    async def spread_plan(request: Request):
        guard(request)
        try:
            return await run_in_threadpool(desk.plan, await request.json())
        except Exception as e:
            return {"ok": False, "errors": [f"{e}"[:300]], "warnings": []}

    @app.post("/api/spread/preview")
    async def spread_preview(request: Request):
        guard(request)
        if demo:
            return DEMO_REFUSAL
        return await run_in_threadpool(desk.preview, await request.json())

    @app.post("/api/spread/submit")
    async def spread_submit(request: Request):
        guard(request)
        if demo:
            return DEMO_REFUSAL
        return await run_in_threadpool(desk.submit, await request.json())

    @app.post("/api/spread/cancel")
    async def spread_cancel(request: Request):
        """Cancel the spread's working order. No arming needed: it only removes risk."""
        guard(request)
        if demo:
            return DEMO_REFUSAL
        return await run_in_threadpool(desk.cancel, str((await request.json()).get("spread_id") or ""))

    @app.post("/api/spread/close_plan")
    async def spread_close_plan(request: Request):
        guard(request)
        body = await request.json()
        try:
            return await run_in_threadpool(desk.close_plan, str(body.get("spread_id") or ""),
                                           str(body.get("entry") or "mid"), body.get("limit_price"))
        except Exception as e:
            return {"ok": False, "errors": [f"{e}"[:300]]}

    @app.post("/api/spread/close")
    async def spread_close(request: Request):
        guard(request)
        if demo:
            return DEMO_REFUSAL
        return await run_in_threadpool(desk.close, await request.json())

    @app.post("/api/spread/target_plan")
    async def spread_target_plan(request: Request):
        guard(request)
        body = await request.json()
        try:
            return await run_in_threadpool(desk.target_plan, str(body.get("spread_id") or ""), body.get("pct"),
                                           body.get("price"))
        except Exception as e:
            return {"ok": False, "errors": [f"{e}"[:300]]}

    @app.post("/api/spread/target")
    async def spread_target(request: Request):
        """Set or move the GTC profit target (an exit: no arming needed, like Close)."""
        guard(request)
        if demo:
            return DEMO_REFUSAL
        return await run_in_threadpool(desk.set_target, await request.json())

    @app.post("/api/spread/cancel_target")
    async def spread_cancel_target(request: Request):
        guard(request)
        if demo:
            return DEMO_REFUSAL
        return await run_in_threadpool(desk.cancel_target, str((await request.json()).get("spread_id") or ""))

    @app.post("/api/spread/stop_plan")
    async def spread_stop_plan(request: Request):
        guard(request)
        body = await request.json()
        try:
            return await run_in_threadpool(desk.stop_plan, str(body.get("spread_id") or ""), body.get("r"),
                                           body.get("price"))
        except Exception as e:
            return {"ok": False, "errors": [f"{e}"[:300]]}

    @app.post("/api/spread/stop")
    async def spread_stop(request: Request):
        """Watch-only stop: set (expected_price), clear or dismiss. Nothing goes to Schwab."""
        guard(request)
        body = await request.json()
        sid, act = str(body.get("spread_id") or ""), str(body.get("action") or "set")
        if act == "set":
            return await run_in_threadpool(desk.set_stop, body)
        if not trader.spreads.get(sid):
            return {"ok": False, "error": "No such spread."}
        if act == "clear":
            if not trader.spreads.stops.clear(sid):
                return {"ok": False, "error": "The stop's close is being sent right now: too late to remove it. "
                                              "Check thinkorswim."}
        elif act == "dismiss":
            trader.spreads.stops.dismiss(sid)
        else:
            return {"ok": False, "error": "unknown stop action"}
        return {"ok": True, "spread_id": sid}

    @app.post("/api/spread/resolve")
    async def spread_resolve(request: Request):
        """Release a companion spread after checking it in thinkorswim (sends nothing)."""
        guard(request)
        if demo:
            return DEMO_REFUSAL
        sid = str((await request.json()).get("spread_id") or "")
        return await run_in_threadpool(trader.resolve_spread, sid, poller._merged_orders())

    return ["spreads"]
