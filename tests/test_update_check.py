import json

from companion import update_check as uc


def _fetch(tag):
    return lambda: {"tag": tag, "url": f"https://example.invalid/{tag}"}


def test_newer_same_and_older(tmp_path, monkeypatch):
    monkeypatch.delenv("EDGE_TRADER_NO_UPDATE_CHECK", raising=False)
    c = tmp_path / "u.json"
    assert uc.check(now=1, cache=c, fetch=_fetch("v1.2.0"), current="1.0.0")["version"] == "v1.2.0"
    c.unlink()
    assert uc.check(now=1, cache=c, fetch=_fetch("v1.0.0"), current="1.0.0") is None
    c.unlink()
    assert uc.check(now=1, cache=c, fetch=_fetch("v0.9.0"), current="1.0.0") is None


def test_cache_hit_skips_the_network(tmp_path, monkeypatch):
    monkeypatch.delenv("EDGE_TRADER_NO_UPDATE_CHECK", raising=False)
    c = tmp_path / "u.json"
    c.write_text(json.dumps({"checked": 1000, "latest": {"tag": "v2.0.0", "url": "x"}}))
    def boom():
        raise AssertionError("network used despite a fresh cache")
    assert uc.check(now=1000 + 3600, cache=c, fetch=boom, current="1.0.0")["version"] == "v2.0.0"


def test_network_error_is_silent_and_opt_out_works(tmp_path, monkeypatch):
    monkeypatch.delenv("EDGE_TRADER_NO_UPDATE_CHECK", raising=False)
    def fail():
        raise OSError("offline")
    assert uc.check(now=1, cache=tmp_path / "u.json", fetch=fail) is None
    monkeypatch.setenv("EDGE_TRADER_NO_UPDATE_CHECK", "1")
    assert uc.check(now=1, cache=tmp_path / "v.json", fetch=_fetch("v9.0.0")) is None
