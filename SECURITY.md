# Security Policy

## Supported versions

Security fixes go into the latest release and the `main` branch.

| Version | Supported |
| --- | --- |
| Latest release | Yes |
| Current `main` branch | Yes |
| Older releases | No |

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's **private vulnerability reporting** from this
repository's Security tab, and include, where you can: what the problem is, how to reproduce it, the
impact, and the files involved. Please do not disclose it publicly until a fix is released.

## What is sensitive here

Edge Trader can **place, change and cancel real orders** in a brokerage account, so these matter most:

- anything that could send an order without the user's click, send it twice, or send the wrong
  symbol, side or size;
- the local server: it listens on `127.0.0.1` only, refuses other host names, and order requests need
  the `X-Companion` header from the app's own page (same origin). A way around any of that is a
  serious report;
- Schwab credentials and tokens (`.env`, `~/.schwabdev/tokens.db`) and the account data in `data/`;
- the floating windows (they start a local process).

## Data and credentials

Never paste your `.env`, tokens, account numbers, the `data` folder or screenshots of a real account
into an issue, a discussion or a pull request. Reproduce problems with `python -m companion --demo`
(invented data, no Schwab connection, no orders) where you can.
