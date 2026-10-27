"""The poller's +1R watcher for Auto B/E tickets: fires once, only on a fresh quote after the fill."""
import time

from companion import poller as poller_mod
from companion import trading
from companion.poller import Poller
from companion.store import Store


class _Trader:
    def __init__(self, store):
        self.store, self.calls = store, []

    def auto_breakeven(self, *args):
        self.calls.append(args)


class _NowThread:
    def __init__(self, target, args, **kw):
        self.target, self.args = target, args

    def start(self):
        self.target(*self.args)


def _setup(tmp_path, monkeypatch):
    store = Store(tmp_path / "p.db")
    p = Poller(store)
    p.trader = _Trader(store)
    monkeypatch.setattr(p, "_fresh", lambda name: True)
    monkeypatch.setattr(p, "_orders_fresh", lambda: True)
    monkeypatch.setattr(trading, "market_open", lambda now=None: True)
    monkeypatch.setattr(poller_mod.threading, "Thread", _NowThread)
    store.add_auto_be("t1", "CRWV")
    return p


def _state(mark, quote_ms=None):
    quote_ms = quote_ms if quote_ms is not None else time.time() * 1000
    # Short 675 CRWV filled 58.39, stop 59.13: +1R = 57.65.
    pos = {"symbol": "CRWV", "side": "SHORT", "qty": 675,
           "r": {"ticket_id": "t1", "side": "SHORT", "entry": 58.39, "stop": 59.13, "rps": 0.74,
                 "first_fill": "2026-10-27T15:33:19+0000"}}
    return {"positions": [pos]}, {"CRWV": {"quote": {"mark": mark, "quoteTime": quote_ms}}}


def test_fires_once_when_the_mark_touches_1r(tmp_path, monkeypatch):
    p = _setup(tmp_path, monkeypatch)
    st, p.quotes = _state(57.70)
    p._auto_be(st)
    assert p.trader.calls == [] and st["positions"][0]["auto_be"]["state"] == "PENDING"
    assert st["positions"][0]["auto_be"]["trigger"] == 57.65
    st, p.quotes = _state(57.65)
    p._auto_be(st)
    assert len(p.trader.calls) == 1 and p.trader.calls[0][:4] == ("t1", "CRWV", "SHORT", 58.39)
    st, p.quotes = _state(57.50)
    p._auto_be(st)
    assert len(p.trader.calls) == 1                     # claimed: never a second move


def test_ignores_a_quote_from_before_the_fill_and_stale_data(tmp_path, monkeypatch):
    p = _setup(tmp_path, monkeypatch)
    st, p.quotes = _state(57.00, quote_ms=1_000)        # quote older than the entry fill
    p._auto_be(st)
    assert p.trader.calls == []
    st, p.quotes = _state(57.00, quote_ms=time.time() * 1000 - 60_000)   # after the fill but a minute old
    p._auto_be(st)
    assert p.trader.calls == []
    st, p.quotes = _state(57.00)
    p.quotes["CRWV"]["realtime"] = False                # delayed quote
    p._auto_be(st)
    assert p.trader.calls == []
    monkeypatch.setattr(p, "_orders_fresh", lambda: False)
    st, p.quotes = _state(57.00)
    p._auto_be(st)
    assert p.trader.calls == [] and p.trader.store.auto_be("t1")["state"] == "PENDING"
