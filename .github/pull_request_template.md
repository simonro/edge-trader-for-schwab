## What this changes

<!-- What and why, in a few sentences. Link the issue if there is one. -->

## How it was tested

<!-- Commands run, and what you checked by hand. -->

- [ ] `python -m pytest -q` passes
- [ ] Order logic changes have tests with the fake broker in `tests/test_trading.py`
- [ ] Nothing places, changes or cancels an order without a user click

## Worth knowing for review

- [ ] New dependency (which, and why)
- [ ] New network call, open port, or background job
- [ ] File access outside the app folder
- [ ] Database change (existing databases keep working)
- [ ] None of the above

No real data, account numbers, `.env` files or databases are included.
