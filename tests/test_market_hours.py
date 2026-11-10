"""Regular-session checks follow Schwab's own hours: market holidays and early closes (the day after
Thanksgiving closes at 13:00 ET), never wider than weekday 9:30 to 16:00 ET."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from companion import trading
from companion.store import Store
from companion.trading import Trader, market_open, regular_hours_text, set_market_hours

ET = ZoneInfo("America/New_York")


def et(day: str, hm: str) -> datetime:
    h, m = map(int, hm.split(":"))
    y, mo, d = map(int, day.split("-"))
    return datetime(y, mo, d, h, m, tzinfo=ET).astimezone(timezone.utc)


def sess(day: str, open_: str, close: str) -> dict:
    return {"date": day, "open": et(day, open_).isoformat(), "close": et(day, close).isoformat()}


@pytest.fixture(autouse=True)
def no_hours(monkeypatch):
    monkeypatch.setattr(trading, "_SCHWAB_DAY", None)


def test_plain_rule_until_schwab_hours_load():
    assert market_open(et("2026-11-27", "14:00"))
    assert not market_open(et("2026-11-27", "16:00"))
    assert not market_open(et("2026-11-28", "11:00"))          # Saturday
    assert regular_hours_text(et("2026-11-27", "14:00")) == "Regular hours only (9:30 to 16:00 ET)."


def test_early_close_day_after_thanksgiving():
    set_market_hours("2026-11-27", sess("2026-11-27", "09:30", "13:00"))
    assert market_open(et("2026-11-27", "12:59"))
    assert not market_open(et("2026-11-27", "13:00"))
    assert not market_open(et("2026-11-27", "15:00"))
    assert "9:30 to 1:00 ET today, early close" in regular_hours_text(et("2026-11-27", "13:30"))


def test_thanksgiving_closed_all_day():
    set_market_hours("2026-11-26", None)
    assert not market_open(et("2026-11-26", "11:00"))
    assert regular_hours_text(et("2026-11-26", "11:00")) == "The market is closed today (Schwab's hours)."


def test_hours_for_another_day_do_not_apply():
    set_market_hours("2026-11-26", None)                          # yesterday's read, not refreshed yet
    assert market_open(et("2026-11-27", "11:00"))


def test_never_wider_than_the_plain_session():
    set_market_hours("2026-11-25", sess("2026-11-25", "08:00", "17:00"))
    assert not market_open(et("2026-11-25", "09:00"))
    assert not market_open(et("2026-11-25", "16:30"))
    assert market_open(et("2026-11-25", "10:00"))


def test_unreadable_hours_fall_back_to_the_plain_rule():
    set_market_hours("2026-11-25", {"open": "garbage"})
    assert market_open(et("2026-11-25", "15:00"))


def test_arming_lapses_at_an_early_close(tmp_path, monkeypatch):
    t = Trader(Store(tmp_path / "t.db"))
    now = {"v": et("2026-11-27", "12:00")}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now["v"].astimezone(tz) if tz else now["v"]

    monkeypatch.setattr(trading, "datetime", Clock)
    set_market_hours("2026-11-27", sess("2026-11-27", "09:30", "13:00"))
    t.arm(True)
    assert t.armed
    now["v"] = et("2026-11-27", "13:01")
    assert not t.armed
    assert "DISARM (session close)" in t.store.get_setting("trading_arm_log")


def test_arming_on_a_normal_day_lasts_to_1600(tmp_path, monkeypatch):
    t = Trader(Store(tmp_path / "t.db"))
    now = {"v": et("2026-11-25", "12:00")}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now["v"].astimezone(tz) if tz else now["v"]

    monkeypatch.setattr(trading, "datetime", Clock)
    set_market_hours("2026-11-25", sess("2026-11-25", "09:30", "16:00"))
    t.arm(True)
    now["v"] = et("2026-11-25", "15:59")
    assert t.armed
    now["v"] = et("2026-11-25", "16:00")
    assert not t.armed


def _clock(monkeypatch, start):
    now = {"v": start}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now["v"].astimezone(tz) if tz else now["v"]

    monkeypatch.setattr(trading, "datetime", Clock)
    return now


def test_orders_refuse_on_a_holiday_with_the_real_check(tmp_path, monkeypatch):
    t = Trader(Store(tmp_path / "t.db"))
    _clock(monkeypatch, et("2026-11-26", "11:00"))                  # Thanksgiving, inside 9:30-16:00
    set_market_hours("2026-11-26", None)
    r = t.close_position("AAPL", [], set())
    assert not r["ok"] and r["error"] == "The market is closed today (Schwab's hours)."


def test_orders_refuse_after_an_early_close_with_the_real_check(tmp_path, monkeypatch):
    t = Trader(Store(tmp_path / "t.db"))
    _clock(monkeypatch, et("2026-11-27", "14:00"))
    set_market_hours("2026-11-27", sess("2026-11-27", "09:30", "13:00"))
    r = t.close_position("AAPL", [], set())
    assert not r["ok"] and "early close" in r["error"]


def test_arming_overnight_lapses_at_the_next_early_close(tmp_path, monkeypatch):
    t = Trader(Store(tmp_path / "t.db"))
    now = _clock(monkeypatch, et("2026-11-26", "18:00"))          # armed Thanksgiving evening
    t.arm(True)
    set_market_hours("2026-11-27", sess("2026-11-27", "09:30", "13:00"))   # Friday's hours load next morning
    now["v"] = et("2026-11-27", "12:59")
    assert t.armed
    now["v"] = et("2026-11-27", "13:00")
    assert not t.armed


def test_arming_given_after_an_early_close_lasts_to_the_next_session(tmp_path, monkeypatch):
    t = Trader(Store(tmp_path / "t.db"))
    now = _clock(monkeypatch, et("2026-11-27", "13:01"))          # Friday, after the 13:00 close
    set_market_hours("2026-11-27", sess("2026-11-27", "09:30", "13:00"))
    t.arm(True)
    assert t.armed
    now["v"] = et("2026-11-28", "10:00")                          # the poller rolls over to Saturday
    set_market_hours("2026-11-28", None)
    assert t.armed
    now["v"] = et("2026-11-30", "15:59")                          # Monday, the session it was armed for
    set_market_hours("2026-11-30", sess("2026-11-30", "09:30", "16:00"))
    assert t.armed
    now["v"] = et("2026-11-30", "16:00")
    assert not t.armed


def test_offset_free_times_are_unreadable_not_a_session():
    set_market_hours("2026-11-27", {"open": "2026-11-27T09:30:00", "close": "2026-11-27T13:00:00"})
    assert market_open(et("2026-11-27", "14:00"))                 # plain rule, and no crash
    p = _poller({"*": {"equity": {"EQ": {"isOpen": True, "sessionHours": {"regularMarket": [
        {"start": "2026-11-27T09:30:00", "end": "2026-11-27T13:00:00"}]}}}}})
    assert not p._fetch_hours("hours", et("2026-11-27", "10:00"))


class _HoursClient:
    def __init__(self, answers):
        self.answers = answers

    def market_hours(self, _markets, date):
        return self.answers.get(date, self.answers.get("*"))


def _poller(answers):
    from companion.poller import Poller
    p = Poller.__new__(Poller)
    p.client, p.hours = _HoursClient(answers), {"kept": True}
    p._call = lambda name, fn: fn()
    return p


OPEN_DAY = {"equity": {"EQ": {"isOpen": True, "sessionHours": {"regularMarket": [
    {"start": "2026-11-27T09:30:00-05:00", "end": "2026-11-27T13:00:00-05:00"}]}}}}


def test_poller_reads_an_early_close_and_publishes_it():
    p = _poller({"2026-11-27": OPEN_DAY})
    assert p._fetch_hours("hours", et("2026-11-27", "10:00"))
    assert p.hours["today"]["close"] == "2026-11-27T13:00:00-05:00"
    assert not market_open(et("2026-11-27", "13:30"))


def test_poller_reads_a_holiday():
    p = _poller({"*": {"equity": {"equity": {"isOpen": False}}}})
    assert p._fetch_hours("hours", et("2026-11-26", "10:00"))
    assert p.hours["today"] is None
    assert not market_open(et("2026-11-26", "11:00"))                 # but today was published as closed


def test_poller_never_takes_a_malformed_answer_for_a_holiday():
    for bad in ({}, {"equity": {}}, {"equity": {"EQ": {"isOpen": True}}}, {"equity": {"EQ": {"date": "x"}}}):
        p = _poller({"*": bad})
        assert not p._fetch_hours("hours", et("2026-11-25", "10:00"))
        assert p.hours == {"kept": True}
        assert market_open(et("2026-11-25", "11:00"))                 # nothing published: plain rule
