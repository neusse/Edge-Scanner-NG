"""Alpaca's batched startup uses only missing ranges for stale symbols."""
from datetime import date, timedelta
from types import SimpleNamespace

import pandas as pd

from scanner.data.alpaca import AlpacaFeed


class _BarsClient:
    def __init__(self):
        self.calls = []

    def get_stock_bars(self, request):
        symbols = request.symbol_or_symbols
        if isinstance(symbols, str):
            symbols = [symbols]
        start, end = request.start.date(), request.end.date()
        self.calls.append((tuple(symbols), start, end))
        rows = []
        for symbol in symbols:
            day = start
            while day <= end:
                if day.weekday() < 5:
                    rows.append((symbol, pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=15)))
                day += timedelta(days=1)
        index = pd.MultiIndex.from_tuples(rows, names=["symbol", "timestamp"])
        return SimpleNamespace(df=pd.DataFrame({
            "open": [10.] * len(rows), "high": [11.] * len(rows),
            "low": [9.] * len(rows), "close": [10.] * len(rows),
            "volume": [100.] * len(rows), "vwap": [10.] * len(rows),
            "trade_count": [1.] * len(rows),
        }, index=index))


def _feed(tmp_path, monkeypatch, *, force=False):
    monkeypatch.setenv("ALPACA_API_KEY", "test")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test")
    feed = AlpacaFeed(cache_dir=tmp_path / "d", intraday_cache_dir=tmp_path / "m",
                      force_refresh_history=force)
    feed._client = _BarsClient()
    return feed


def test_batched_restart_groups_symbols_by_missing_range(tmp_path, monkeypatch):
    feed = _feed(tmp_path, monkeypatch)
    start, first_end, next_end = date(2026, 1, 5), date(2026, 1, 8), date(2026, 1, 9)
    first = feed.get_historical_daily_multi(["A", "B"], start, first_end)
    assert len(feed._client.calls) == 1
    second = feed.get_historical_daily_multi(["A", "B", "C"], start, next_end)
    assert feed._client.calls[1:] == [
        (("A", "B"), first_end, next_end), (("C",), start, next_end),
    ] or feed._client.calls[1:] == [
        (("C",), start, next_end), (("A", "B"), first_end, next_end),
    ]
    assert all(len(second[s]) == len(first[s]) + 1 for s in ("A", "B"))
    assert all(second[s].index.is_unique for s in ("A", "B", "C"))
    feed.get_historical_daily_multi(["A", "B", "C"], start, next_end)
    assert len(feed._client.calls) == 3  # fully current cache: zero requests


def test_force_refresh_downloads_full_window(tmp_path, monkeypatch):
    feed = _feed(tmp_path, monkeypatch)
    start, end = date(2026, 1, 5), date(2026, 1, 9)
    feed.get_historical_daily_multi(["A"], start, end)
    forced = _feed(tmp_path, monkeypatch, force=True)
    forced.get_historical_daily_multi(["A"], start, end)
    assert forced._client.calls == [(("A",), start, end)]
