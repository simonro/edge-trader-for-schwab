"""Once-a-day check for a newer release on GitHub. Notify only: never downloads or installs.

It asks https://api.github.com/repos/<repo>/releases/latest (nothing is sent but GitHub's own
defaults), caches the answer for a day in data/update_check.json, gives up after 3 seconds, and is
silent on any error or when offline. Turn it off with EDGE_TRADER_NO_UPDATE_CHECK=1.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import __version__

REPO = "simonro/edge-trader-for-schwab"
URL = f"https://api.github.com/repos/{REPO}/releases/latest"
CACHE = Path(__file__).resolve().parent.parent / "data" / "update_check.json"
DAY = 86400


def _parse(v: str) -> tuple[int, ...]:
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums[:3]) if nums else (0,)


def newer(latest: str, current: str = __version__) -> bool:
    return _parse(latest) > _parse(current)


def _fetch(timeout: float = 3.0) -> dict | None:
    import requests
    r = requests.get(URL, timeout=timeout, headers={"Accept": "application/vnd.github+json"})
    if r.status_code != 200:
        return None
    d = r.json()
    return {"tag": d.get("tag_name"), "url": d.get("html_url"), "name": d.get("name")}


def check(now: float | None = None, cache: Path = CACHE, fetch=_fetch, current: str = __version__) -> dict | None:
    """{"version", "url", "current"} when a newer release exists, else None. Never raises."""
    if os.environ.get("EDGE_TRADER_NO_UPDATE_CHECK") == "1":
        return None
    now = time.time() if now is None else now
    try:
        cached = json.loads(cache.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cached = None
    latest = None
    if cached and now - cached.get("checked", 0) < DAY:
        latest = cached.get("latest")
    else:
        try:
            latest = fetch()
        except Exception:
            latest = None
        if latest is not None:
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"checked": now, "latest": latest}), encoding="utf-8")
            except OSError:
                pass
    if latest and latest.get("tag") and newer(latest["tag"], current):
        return {"version": latest["tag"], "url": latest.get("url"), "current": current}
    return None
