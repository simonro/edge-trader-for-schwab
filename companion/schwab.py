"""Schwab client factory, built on schwabdev (https://github.com/tylerebowers/Schwabdev, MIT).

Keys come from `.env` in the app folder (SCHWAB_APP_KEY, SCHWAB_APP_SECRET, SCHWAB_CALLBACK_URL);
SCHWAB_ENV_FILE can point to another .env to share keys with another app. Tokens live in
~/.schwabdev/tokens.db (SCHWAB_TOKENS_DB overrides); schwabdev coordinates refreshes across
processes, so apps sharing the token file can run at once. `python -m companion.login` signs in.

Never opens a streamer: Schwab allows one per user and another app (a scanner, a charting tool)
may hold it. REST polling only.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
SHARED_ENV = Path(os.path.expanduser(os.environ["SCHWAB_ENV_FILE"])) if os.environ.get("SCHWAB_ENV_FILE") else None
if SHARED_ENV:
    load_dotenv(SHARED_ENV)       # keys shared with another app (optional)

TOKENS_DB = Path(os.path.expanduser(os.environ.get("SCHWAB_TOKENS_DB", "~/.schwabdev/tokens.db")))
REFRESH_TOKEN_DAYS = 7
# schwabdev starts an interactive browser login when the refresh token has
# under ~60 min left. A background server must never reach that prompt.
SAFETY_HOURS = 2

# The only schwabdev.Client methods the poller may call. Everything else
# (place/replace/cancel/preview order, streamer, ...) is refused.
_ALLOWED = frozenset({
    "linked_accounts", "account_details", "account_details_all",
    "account_orders", "account_orders_all", "order_details",
    "transactions", "transaction_details",
    "quote", "quotes", "price_history", "market_hours", "market_hour",
})
# The order client: preview and place orders, cancel and replace (stop moves). Every call it
# makes comes from a click in the dashboard.
_TRADING = frozenset({"linked_accounts", "account_details", "account_orders", "quote", "quotes",
                      "order_details", "preview_order", "place_order", "cancel_order",
                      "replace_order"})   # replace: only Trader.move_stop (stop price of a live stop)


class AuthExpired(RuntimeError):
    pass


def refresh_token_age_days(db: Path = TOKENS_DB) -> float | None:
    """Days since Schwab issued the refresh token, or None. Reads only the timestamp."""
    try:
        with sqlite3.connect(db) as con:
            row = con.execute("select refresh_token_issued from schwabdev").fetchone()
    except sqlite3.Error:
        return None
    if not row:
        return None
    issued = datetime.fromisoformat(row[0])
    if issued.tzinfo is None:
        issued = issued.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - issued).total_seconds() / 86400


def auth_hours_left() -> float | None:
    age = refresh_token_age_days()
    return None if age is None else (REFRESH_TOKEN_DAYS - age) * 24


class ReadOnlyClient:
    """Wraps schwabdev.Client and exposes only the methods in its allowlist."""

    def __init__(self, inner, allowed: frozenset[str] = _ALLOWED):
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_allowed", allowed)

    def __getattr__(self, name):
        if name not in self._allowed:
            raise PermissionError(f"Edge Trader for Schwab: {name} is not allowed on this client")
        return getattr(self._inner, name)

    def __setattr__(self, name, value):
        raise PermissionError("ReadOnlyClient is immutable")


def make_trading_client() -> ReadOnlyClient:
    """Separate client for the order ticket (preview + place only)."""
    return make_client(_TRADING)


def make_client(allowed: frozenset[str] = _ALLOWED) -> ReadOnlyClient:
    left = auth_hours_left()
    if left is None or left < SAFETY_HOURS:
        raise AuthExpired(
            "Schwab login expired or expiring. Run login.bat (python -m companion.login), "
            "then restart Edge Trader.")
    import schwabdev

    def _no_prompt(*_a, **_k):
        raise AuthExpired("Schwab needs a browser login: run login.bat (python -m companion.login)")

    key = os.environ.get("SCHWAB_APP_KEY")
    secret = os.environ.get("SCHWAB_APP_SECRET")
    if not key or not secret:
        raise RuntimeError("SCHWAB_APP_KEY / SCHWAB_APP_SECRET not found: set them in .env (see .env.example)")
    inner = schwabdev.Client(
        app_key=key,
        app_secret=secret,
        callback_url=os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1"),
        tokens_db=str(TOKENS_DB),
        call_on_auth=_no_prompt,
        open_browser_for_auth=False,
    )
    # schwabdev retries 429/5xx three times with backoff inside one call. That hides
    # requests from the budget and blocks the poller, and a retried POST could place an
    # order twice. No automatic retries anywhere.
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    inner._session.mount("https://", HTTPAdapter(max_retries=Retry(total=0, raise_on_status=False)))
    return ReadOnlyClient(inner, allowed)
