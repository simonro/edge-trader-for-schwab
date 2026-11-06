"""Watch-only spread stop (slice 4): never sends, fires on two distinct observations."""
from datetime import datetime, timezone

import pytest

from companion.spreads.spread_stop import StopWatch
from companion.store import Store

SHORT, LONG = "QCOM  261106C00157500", "QCOM  261106C00160000"
T0 = datetime(2026, 11, 4, 19, 0, tzinfo=timezone.utc).timestamp()


def spread(state="OPEN", kind="CALL_CREDIT"):
    credit = kind.endswith("CREDIT")
    return {"spread_id": "sp-1", "kind": kind, "state": state, "created": "2026-11-04T18:15:00+00:00",
            "legs": [{"symbol": SHORT, "side": "SHORT" if credit else "LONG", "ratio": 1},
                     {"symbol": LONG, "side": "LONG" if credit else "SHORT", "ratio": 1}]}


def quotes(near, far, t, realtime=True):
    """near / far as (bid, ask); t: Schwab's quote time in seconds."""
    return {SHORT: {"quote": {"bidPrice": near[0], "askPrice": near[1], "quoteTime": t * 1000}, "realtime": realtime},
            LONG: {"quote": {"bidPrice": far[0], "askPrice": far[1], "quoteTime": t * 1000}, "realtime": realtime}}


@pytest.fixture
def w(tmp_path):
    s = StopWatch(Store(tmp_path / "s.db"))
    s.set("sp-1", 1.85, 0.5)
    return s


def look(w, q, t, sp=None, market=True):
    return w.watch(sp or spread(), q.get, market, now=t + 1)


def test_no_stop_no_status(tmp_path):
    assert StopWatch(Store(tmp_path / "s.db")).watch(spread(), {}.get, True) is None


def test_below_the_stop_watches_and_says_how_far(w):
    st = look(w, quotes((5.40, 5.50), (4.30, 4.40), T0), T0)     # mid 1.10
    assert st["status"] == "watching" and st["mid"] == 1.10 and "0.75 from the stop" in st["detail"]
    assert st["watch_only"] is True


def test_fires_only_on_a_second_distinct_observation_at_least_2_5_s_later(w):
    q = quotes((6.10, 6.20), (4.20, 4.30), T0)                   # mid 1.90: at the stop (credit: cost rose)
    assert look(w, q, T0)["detail"].endswith("(1 of 2)")
    assert look(w, q, T0 + 3)["status"] == "watching"            # same Schwab quote again: not a new observation
    assert look(w, quotes((6.10, 6.20), (4.20, 4.30), T0 + 1), T0 + 1)["status"] == "watching"   # too soon
    st = look(w, quotes((6.10, 6.20), (4.20, 4.30), T0 + 3), T0 + 3)
    assert st["status"] == "fired" and st["fired_mid"] == 1.90 and st["fired_at"]
    assert [e["kind"] for e in w.events("sp-1")] == ["set", "would_fire"]


def test_dropping_off_the_stop_resets_the_count(w):
    look(w, quotes((6.10, 6.20), (4.20, 4.30), T0), T0)
    look(w, quotes((5.40, 5.50), (4.30, 4.40), T0 + 2), T0 + 2)  # back below
    assert look(w, quotes((6.10, 6.20), (4.20, 4.30), T0 + 4), T0 + 4)["detail"].endswith("(1 of 2)")


@pytest.mark.parametrize("q, why", [
    (quotes((6.10, 6.20), (4.20, 4.30), T0, realtime=False), "real time"),
    (quotes((6.10, 6.20), (4.20, 4.30), T0, realtime=None), "real time"),
    (quotes((6.10, 6.20), (0.0, 0.05), T0), "no bid"),
    (quotes((6.30, 6.20), (4.20, 4.30), T0), "invalid market"),
])
def test_bad_quotes_suspend_and_reset(w, q, why):
    look(w, quotes((6.10, 6.20), (4.20, 4.30), T0 - 5), T0 - 5)
    st = look(w, q, T0)
    assert st["status"] == "suspended" and why in st["detail"]
    assert look(w, quotes((6.10, 6.20), (4.20, 4.30), T0 + 3), T0 + 3)["detail"].endswith("(1 of 2)")


def test_stale_or_skewed_or_pre_spread_quotes_suspend(w):
    assert "old" in w.watch(spread(), quotes((6.10, 6.20), (4.20, 4.30), T0).get, True, now=T0 + 20)["detail"]
    q = quotes((6.10, 6.20), (4.20, 4.30), T0)
    q[LONG]["quote"]["quoteTime"] = (T0 - 12) * 1000
    assert "too far apart" in look(w, q, T0)["detail"]
    early = datetime(2026, 11, 4, 18, 0, tzinfo=timezone.utc).timestamp()
    assert "newer than the spread" in look(w, quotes((6.10, 6.20), (4.20, 4.30), early), early)["detail"]


def test_paused_when_not_open_or_after_hours(w):
    q = quotes((6.10, 6.20), (4.20, 4.30), T0)
    assert look(w, q, T0, sp=spread(state="NEEDS_ACTION"))["status"] == "paused"
    assert look(w, q, T0, sp=spread(state="CLOSING"))["status"] == "watching"      # a resting close keeps the watch
    assert look(w, q, T0, market=False)["detail"] == "outside regular hours"


def test_a_debit_spread_stops_when_its_value_falls(tmp_path):
    w = StopWatch(Store(tmp_path / "s.db"))
    w.set("sp-1", 0.60, 0.5)
    sp = spread(kind="CALL_DEBIT")                                # long the 157.5, short the 160
    assert look(w, quotes((5.40, 5.50), (4.30, 4.40), T0), T0, sp=sp)["status"] == "watching"
    look(w, quotes((4.80, 4.90), (4.30, 4.40), T0), T0, sp=sp)    # mid 0.50
    assert look(w, quotes((4.80, 4.90), (4.30, 4.40), T0 + 3), T0 + 3, sp=sp)["status"] == "fired"


def test_dismiss_keeps_watching_and_clear_removes(w):
    look(w, quotes((6.10, 6.20), (4.20, 4.30), T0), T0)
    assert look(w, quotes((6.10, 6.20), (4.20, 4.30), T0 + 3), T0 + 3)["status"] == "fired"
    w.dismiss("sp-1")
    assert look(w, quotes((6.10, 6.20), (4.20, 4.30), T0 + 6), T0 + 6)["detail"].endswith("(1 of 2)")
    w.clear("sp-1")
    assert look(w, quotes((6.10, 6.20), (4.20, 4.30), T0 + 9), T0 + 9) is None


def test_a_first_hit_before_a_polling_gap_does_not_confirm_after_it(w):
    look(w, quotes((6.10, 6.20), (4.20, 4.30), T0), T0)
    st = look(w, quotes((6.10, 6.20), (4.20, 4.30), T0 + 30), T0 + 30)     # 30 s later: a gap, start over
    assert st["status"] == "watching" and st["detail"].endswith("(1 of 2)")


def test_a_look_at_the_old_stop_never_fires_a_moved_one(w):
    look(w, quotes((6.10, 6.20), (4.20, 4.30), T0), T0)                   # 1 of 2 against 1.85
    old = w.get("sp-1")
    real_get = w.get
    w.set("sp-1", 2.20, 0.8)                                               # moved while the poller looks
    w.get = lambda sid: old                                                # the poller read the old stop
    st = look(w, quotes((6.10, 6.20), (4.20, 4.30), T0 + 3), T0 + 3)
    w.get = real_get
    assert st["status"] != "fired" and w.get("sp-1")["fired_at"] is None
