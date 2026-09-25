"""Scheduled refresh reuses caches without creating a Schwab stream."""
from __future__ import annotations

import json
import socket
from datetime import date

import pandas as pd
import pytest

from scripts import refresh_schwab_history as job


def test_dry_run_has_no_auth_or_cache_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/universe.csv").write_text("symbol\nAAPL\nMSFT\n", encoding="utf-8")
    result = job.refresh(session=date(2026, 9, 23), dry_run=True)
    assert result["status"] == "dry_run" and result["symbols"] == 2
    assert not (tmp_path / "data/.scanner-instance.lock").exists()


def test_refresh_calls_daily_and_five_minute_history_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import scripts.run_live as live
    import scripts.import_schwab_py_token as importer
    import scanner.data.schwab as schwab

    monkeypatch.setattr(live, "_ensure_universe", lambda *_: ["AAPL", "MSFT"])
    monkeypatch.setattr(live, "_ensure_sector_map", lambda *_: ({"AAPL": "XLK"}, ["XLK"]))
    monkeypatch.setattr(importer, "main", lambda: None)
    monkeypatch.setattr(job, "load_active_universe", lambda: None)
    calls = []
    frame = pd.DataFrame({"open": [1], "high": [2], "low": [1], "close": [2], "volume": [100]},
                         index=pd.DatetimeIndex(["2026-09-23T20:00:00Z"]))

    class FakeFeed:
        def __init__(self):
            self._history_stats = {"daily": {"current": 1, "incremental": 3, "full": 0, "requests": 3},
                                   "5m": {"current": 1, "incremental": 1, "full": 0, "requests": 1}}

        def get_historical_daily_multi(self, symbols, start, end, progress):
            calls.append(("daily", symbols, end))
            return {s: frame for s in symbols}

        def get_historical_bars_multi(self, symbols, timeframe, start, end, progress):
            calls.append(("5min", symbols, timeframe, end))
            return {s: frame for s in symbols}

        def subscribe_minute_bars(self, *_):
            raise AssertionError("refresh must not create a stream")

    monkeypatch.setattr(schwab, "SchwabFeed", FakeFeed)
    result = job.refresh(session=date(2026, 9, 23), live_port=0,
                         result_path=tmp_path / "result.json")
    assert result["status"] == "ok"
    assert calls == [("daily", ["SPY", "XLK", "AAPL", "MSFT"], date(2026, 9, 23)),
                     ("5min", ["AAPL", "MSFT"], "5Min", date(2026, 9, 23))]
    assert json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))["symbols"] == 2


def test_refresh_refuses_live_listener_before_auth(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        with pytest.raises(RuntimeError, match="still listening"):
            job.refresh(session=date(2026, 9, 23), live_port=sock.getsockname()[1])
    assert not (tmp_path / "data/schwab/history_refresh.json").exists()


def test_refresh_rejects_before_close():
    with pytest.raises(ValueError, match="not ready"):
        job._target_session(pd.Timestamp("2026-09-24T19:00:00Z").to_pydatetime(), date(2026, 9, 24))
