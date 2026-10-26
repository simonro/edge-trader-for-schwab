import os

from companion import schwab


def test_shared_env_fills_blank_keys_but_keeps_set_ones(tmp_path, monkeypatch):
    shared = tmp_path / "shared.env"
    shared.write_text("SCHWAB_APP_KEY=shared-key\nSCHWAB_APP_SECRET=shared-secret\nSCHWAB_CALLBACK_URL=https://other\n")
    monkeypatch.setenv("SCHWAB_APP_KEY", "")                   # blank line copied from .env.example
    monkeypatch.delenv("SCHWAB_APP_SECRET", raising=False)
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
    schwab._fill_from(shared)
    assert os.environ["SCHWAB_APP_KEY"] == "shared-key"
    assert os.environ["SCHWAB_APP_SECRET"] == "shared-secret"
    assert os.environ["SCHWAB_CALLBACK_URL"] == "https://127.0.0.1"   # our own value wins
