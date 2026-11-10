"""The journal sync's endpoints and page script, registered by the app only when this package exists.

Every POST runs on the trader's click (the page's X-Companion guard); nothing here places, changes
or cancels an order, and nothing reaches the journal before Preview and Apply.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse

from .sync import Journal, JournalError

STATIC = Path(__file__).resolve().parent / "static"


def register(app, store, guard, demo: bool = False) -> list[str]:
    """Add the journal endpoints; returns the page scripts the dashboard should load."""
    journal = Journal(store, demo=demo)

    def _jcall(fn, *args):
        try:
            out = fn(*args)
            return {"ok": True, **out} if isinstance(out, dict) else {"ok": True, "result": out}
        except JournalError as e:
            return {"ok": False, "error": str(e)}
        except (TypeError, ValueError, KeyError) as e:
            return {"ok": False, "error": f"Bad request: {e}"[:300]}


    @app.get("/features/journal.js", include_in_schema=False)
    def journal_js():
        return FileResponse(STATIC / "journal.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})


    @app.get("/api/journal/status")
    def journal_status():
        return journal.status()


    @app.get("/api/journal/settings")
    def journal_settings():
        return journal.settings()


    @app.post("/api/journal/settings")
    async def journal_settings_set(request: Request):
        guard(request)
        # Off the event loop: a running Preview/Apply holds the journal lock for up to 2 minutes.
        return await run_in_threadpool(_jcall, journal.save_settings, await request.json())


    @app.post("/api/journal/test")
    async def journal_test(request: Request):
        """Check a journal address (the saved one when none is given): sync version and accounts."""
        guard(request)
        body = await request.json()
        url = str(body.get("url") or "").strip().rstrip("/") or None
        return await run_in_threadpool(_jcall, journal.test, url, body.get("allow_remote") is True)


    @app.post("/api/journal/library")
    async def journal_library(request: Request):
        guard(request)
        return await run_in_threadpool(_jcall, journal.library)


    @app.post("/api/journal/entry/get")
    async def journal_entry_get(request: Request):
        guard(request)
        return _jcall(journal.get_entry, list((await request.json()).get("fill_ids") or []))


    @app.post("/api/journal/entry")
    async def journal_entry_save(request: Request):
        guard(request)
        b = await request.json()
        return _jcall(journal.save_entry, str(b.get("symbol") or ""), list(b.get("fill_ids") or []),
                      b.get("fields"), b.get("tags"))


    @app.post("/api/journal/note")
    async def journal_note(request: Request):
        guard(request)
        b = await request.json()
        return _jcall(journal.add_note, str(b.get("symbol") or ""), list(b.get("fill_ids") or []), b.get("text"))


    @app.post("/api/journal/note/edit")
    async def journal_note_edit(request: Request):
        guard(request)
        b = await request.json()
        return _jcall(journal.edit_note, str(b.get("id") or ""), b.get("text"))


    @app.post("/api/journal/day_note/get")
    async def journal_day_note_get(request: Request):
        guard(request)
        return _jcall(journal.day_note, str((await request.json()).get("date") or ""))


    @app.post("/api/journal/day_note")
    async def journal_day_note(request: Request):
        guard(request)
        b = await request.json()
        return _jcall(journal.save_day_note, str(b.get("date") or ""), b.get("text"))


    @app.post("/api/journal/preview")
    async def journal_preview(request: Request):
        """Dry run: what the journal would add and change. Nothing is written."""
        guard(request)
        return await run_in_threadpool(_jcall, journal.preview)


    @app.post("/api/journal/account")
    async def journal_account(request: Request):
        """Send one trade's fills to another journal account (account_id null: back to its route)."""
        guard(request)
        b = await request.json()
        return await run_in_threadpool(_jcall, journal.set_account, list(b.get("fill_keys") or []), b.get("account_id"))


    @app.post("/api/journal/apply")
    async def journal_apply(request: Request):
        """Send exactly the previewed sync. The journal refuses if it changed since the preview."""
        guard(request)
        return await run_in_threadpool(_jcall, journal.apply, str((await request.json()).get("token") or ""))

    return ["journal"]
