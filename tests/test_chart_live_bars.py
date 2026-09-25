"""Chart history is warmed once; the scanner's live bars advance the chart."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi.testclient import TestClient

from scanner.api import AppState, create_app
from scanner.live_scanner import LiveScanner
from tests.test_live_scanner import _FakeFeed, _warmup


ET = ZoneInfo("America/New_York")


class ChartFeed(_FakeFeed):
    def __init__(self):
        super().__init__()
        self.history_calls = []

    def get_bars_range(self, symbol, timeframe, start, end):
        self.history_calls.append((symbol, timeframe))
        stamp = pd.Timestamp(datetime.now(ET).replace(hour=9, minute=30, second=0, microsecond=0))
        return pd.DataFrame(
            {"open": [100.0], "high": [101.0], "low": [99.0], "close": [100.5], "volume": [1000.0]},
            index=pd.DatetimeIndex([stamp]),
        )


def _live_bar(minute, *, source="schwab_chart_equity"):
    stamp = pd.Timestamp(datetime.now(ET).replace(hour=10, minute=minute, second=0, microsecond=0))
    return {"symbol": "AAPL", "timestamp": stamp.tz_convert("UTC"),
            "open": 101.0, "high": 102.0, "low": 100.0, "close": 101.5,
            "volume": 200.0, "source": source}


def test_chart_uses_seeded_and_live_bars_without_refetching_history():
    feed = ChartFeed()
    scanner = LiveScanner(["AAPL"], feed)
    _warmup(scanner)
    scanner.seed_session_bar(_live_bar(1, source="schwab_history_1m"))
    client = TestClient(create_app(AppState(scanner=scanner, feed=feed)))

    first = client.get("/api/bars/AAPL/1min")
    assert first.status_code == 200
    assert [b["c"] for b in first.json()["bars"]][-2:] == [100.5, 101.5]
    assert feed.history_calls == [("AAPL", "1Min")]

    scanner._on_bar(_live_bar(2, source="schwab_quote_derived_1m"))
    second = client.get("/api/bars/AAPL/1min")
    assert second.status_code == 200
    assert [b["t"][11:16] for b in second.json()["bars"]][-2:] == ["10:01", "10:02"]
    assert feed.history_calls == [("AAPL", "1Min")]


def test_five_minute_chart_aggregates_new_streamed_minutes_without_refetch():
    feed = ChartFeed()
    scanner = LiveScanner(["AAPL"], feed)
    _warmup(scanner)
    client = TestClient(create_app(AppState(scanner=scanner, feed=feed)))
    assert client.get("/api/bars/AAPL/5min").status_code == 200
    scanner._on_bar(_live_bar(1))
    bar = _live_bar(2)
    bar.update({"high": 103.0, "close": 102.5, "volume": 300.0})
    scanner._on_bar(bar)
    result = client.get("/api/bars/AAPL/5min")
    assert result.status_code == 200
    candle = result.json()["bars"][-1]
    assert candle["t"][11:16] == "10:00"
    assert (candle["o"], candle["h"], candle["c"], candle["v"]) == (101.0, 103.0, 102.5, 500.0)
    assert feed.history_calls == [("AAPL", "5Min")]
