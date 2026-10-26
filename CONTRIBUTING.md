# Contributing to Edge Trader for Schwab

Thanks for helping. Bug reports, fixes and features are all welcome. This page covers how to set up,
what to test, and what a pull request needs before it can be merged.

**Security problems:** please do not open a public issue. Follow [SECURITY.md](SECURITY.md) and use
GitHub's private vulnerability reporting.

## What the project cares about

These decide most review questions:

1. **Every order is the user's click.** Nothing places, replaces or cancels an order on its own: no
   automation, no retries of an order request, no background jobs that trade. A change that would act
   without a click will not be merged.
2. **Protection is never removed first.** Schwab cancels a symbol's working orders when a new order
   for it is entered, so exits are replaced by sending the new bracket (Schwab drops the old one),
   never by cancelling the stop and then sending a new one.
3. **An incomplete view never looks complete.** P&L carries its coverage (matched, estimated,
   unresolved, stale); unknown is never shown as zero; a stale connection disables order buttons.
4. **When unsure, refuse.** An order with an unclear outcome blocks further orders for that symbol
   until Schwab confirms it or the user presses Resolve.
5. **REST only.** Never open Schwab's streamer: Schwab allows one per user and another app may hold it.
6. **Local first.** The server listens on `127.0.0.1` only. A new network call needs an issue first.
7. **Small and focused.** One pull request, one change.

## Setting up

Requirements: Python 3.11 or newer. On Windows, run `setup.bat` once and `launch.bat` to start. By hand:

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env            # your own Schwab app key and secret
.venv/bin/python -m companion.login
.venv/bin/python -m companion
```

A new install is always empty. For UI work without a Schwab account, `python -m companion --demo`
shows invented data, never connects to Schwab, refuses every order action and keeps its own throwaway
database (`data/demo.db`). `scripts/capture_screenshots.py` retakes the README screenshots from it.

**Never use or commit real data**: not in tests, fixtures, screenshots or issues.

## Tests

CI runs these on every pull request, and they must pass:

```bash
python -m pytest -q
```

What to add:

- **A fix:** a test that fails without it.
- **Order logic** (`companion/trading.py`, `companion/bracket.py`): tests against the fake broker in
  `tests/test_trading.py` (`Broker` simulates working orders, fills, replaces and Schwab's
  cancel-on-new-order behavior). Cover the unhappy paths: no answer, HTTP 5xx, partial fills, a
  position that changed in between.
- **P&L** (`companion/pnl.py`): fills with hand-worked expected values.
- **A UI change:** a screenshot from `--demo` in the pull request.

## Where things live

- `companion/schwab.py`: the Schwab clients (built on [Schwabdev](https://github.com/tylerebowers/Schwabdev)),
  each with an allowlist of the API methods it may call.
- `companion/poller.py`: the one polling thread and its request budget.
- `companion/view.py`, `companion/orders.py`, `companion/pnl.py`: dashboard state, order trees, the FIFO ledger.
- `companion/bracket.py`: sizing and the bracket order. `companion/trading.py`: every order action.
- `companion/app.py`: the local FastAPI server. `companion/static/index.html`: the whole UI.
- `companion/floating.py`: the always-on-top windows (pywebview).

## Pull requests

- Branch from `main` and keep the change focused.
- Describe what changed and why, and how you tested it.
- Say so if the change adds a dependency, a network call, file access outside the app folder, or a
  database change. New dependencies need a reason.
- Never include `.env` files, tokens, account numbers, databases or real screenshots.

## Code style

Match the code around you. Comments explain why something is done, not what the next line does.
User-facing text is plain and specific: say what happened and what to do next.

## Questions

Ask in [Discussions](https://github.com/simonro/edge-trader-for-schwab/discussions). Never paste real
account details.
