"""Sign in to Schwab and check that Edge Trader can read your account.

    python -m companion.login            (or login.bat)
    python -m companion.login --reauth   (force a brand new sign-in)

Built on schwabdev by Tyler Bowers (https://github.com/tylerebowers/Schwabdev, MIT licence), which
handles Schwab's OAuth sign-in and token refresh.

Run it once after setup and again about once a week: Schwab's refresh token lasts 7 days and can
only be renewed in a browser. Nothing here places, changes or cancels an order.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta

from . import schwab


def main() -> None:
    ap = argparse.ArgumentParser(description="Schwab sign-in for Edge Trader")
    ap.add_argument("--reauth", action="store_true", help="force a new sign-in even if the token is still valid")
    args = ap.parse_args()

    key = os.environ.get("SCHWAB_APP_KEY")
    secret = os.environ.get("SCHWAB_APP_SECRET")
    cb = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
    if not key or not secret:
        sys.exit("SCHWAB_APP_KEY and SCHWAB_APP_SECRET are missing. Copy .env.example to .env and fill them in "
                 "(Schwab developer portal, your app's App Key and Secret).")

    print("\nEdge Trader for Schwab: sign in")
    print(f"  app key      : {key[:4]}...{key[-4:]}")
    print(f"  callback url : {cb}")
    print(f"  token file   : {schwab.TOKENS_DB}")
    print()
    print("  When the browser opens Schwab's page, sign in and approve.")
    print("   1. The page you land on after approving will NOT load (an SSL or 'can't connect' error).")
    print("      That is expected: copy the whole address from the address bar anyway.")
    print("   2. Paste it here right away: the code inside expires in about 30 seconds.")
    print("   3. It must start with https:// (some browsers hide that part when you copy).")
    print()

    import schwabdev
    client = schwabdev.Client(app_key=key, app_secret=secret, callback_url=cb, tokens_db=str(schwab.TOKENS_DB))
    if args.reauth:
        client.tokens.update_tokens(force_refresh_token=True)

    try:
        t = client.tokens
        issued = getattr(t, "_refresh_token_issued", None)
        if issued is not None:
            left = timedelta(seconds=getattr(t, "_refresh_token_timeout", 0)) - (datetime.now(issued.tzinfo) - issued)
            print(f"  Signed in. The sign-in lasts {left.total_seconds() / 86400:.1f} more days; run this again before then.")
    except Exception as exc:  # informational only
        print(f"  (could not read the token expiry: {exc})")

    ok = True
    try:
        r = client.linked_accounts()
        r.raise_for_status()
        n = len(r.json() or [])
        print(f"  [ OK ] linked accounts: {n}")
        if n != 1:
            print("         Edge Trader uses one account. With several linked accounts it trades only the first "
                  "one the dashboard shows.")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] linked accounts: {exc}")
    try:
        r = client.quotes(symbols=["SPY"], fields="quote")
        r.raise_for_status()
        q = (r.json() or {}).get("SPY", {}).get("quote", {})
        print(f"  [ OK ] quotes: SPY last {q.get('lastPrice')}")
    except Exception as exc:
        ok = False
        print(f"  [FAIL] quotes: {exc}")

    print()
    if ok:
        print("  Ready. Start Edge Trader with launch.bat.")
        print("  Before your first order: in the Schwab developer portal, open your app (Modify App) and check")
        print("  its Order Limit is above 0. With 0, every order is refused with HTTP 429.")
    else:
        sys.exit("  Something failed: check the keys in .env and that your app has the Trader API product.")


if __name__ == "__main__":
    main()
