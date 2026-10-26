"""Small always-on-top windows next to the charts: Entry (the New trade ticket) and Manage (the
selected position's workbench).

    pythonw -m companion.floating entry
    pythonw -m companion.floating manage

Each window is the same dashboard page in a compact view (?view=entry / ?view=manage), shown in
a native window (pywebview on Windows' Edge WebView2): no tabs, no address bar, and a pin button
that keeps it on top. It talks only to the local Edge Trader server, so it adds no Schwab requests.
Size and position are remembered per window in data/floating.json.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

PORT = int(os.environ.get("COMPANION_PORT", "8770"))
DATA = Path(__file__).resolve().parent.parent / "data"
GEOM = DATA / "floating.json"
DEFAULTS = {"entry": {"width": 380, "height": 700}, "manage": {"width": 560, "height": 560}}
TITLES = {"entry": "Edge Trader · Entry", "manage": "Edge Trader · Manage"}


def _load() -> dict:
    try:
        return json.loads(GEOM.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(kind: str, geom: dict) -> None:
    allg = _load()
    allg[kind] = geom
    DATA.mkdir(parents=True, exist_ok=True)
    GEOM.write_text(json.dumps(allg, indent=1), encoding="utf-8")


class Api:
    """Called from the page as window.pywebview.api.*"""

    def __init__(self, kind: str):
        self.kind = kind
        self._window = None   # private: pywebview exposes public attributes to the page

    def pin(self, on: bool) -> bool:
        self._window.on_top = bool(on)
        g = _load().get(self.kind, {})
        g["pinned"] = bool(on)
        _save(self.kind, g)
        return self._window.on_top

    def pinned(self) -> bool:
        return bool(self._window.on_top)


def main() -> None:
    import webview
    kind = (sys.argv[1] if len(sys.argv) > 1 else "manage").lower()
    if kind not in DEFAULTS:
        raise SystemExit("usage: python -m companion.floating entry|manage")
    g = {**DEFAULTS[kind], **_load().get(kind, {})}
    api = Api(kind)
    win = webview.create_window(TITLES[kind], f"http://127.0.0.1:{PORT}/?view={kind}", js_api=api,
                                width=g["width"], height=g["height"], x=g.get("x"), y=g.get("y"),
                                on_top=g.get("pinned", True), min_size=(320, 300),
                                background_color="#0b0e13")
    api._window = win

    def remember():
        try:
            _save(kind, {"width": win.width, "height": win.height, "x": win.x, "y": win.y,
                         "pinned": bool(win.on_top)})
        except Exception:
            pass

    win.events.closing += remember
    DATA.mkdir(parents=True, exist_ok=True)
    webview.start(private_mode=False, storage_path=str(DATA / "webview"))


if __name__ == "__main__":
    main()
