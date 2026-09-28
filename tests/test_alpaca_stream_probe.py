"""Offline checks for the standalone, bounded Alpaca stream probe."""
from datetime import datetime, timezone

from scripts.probe_alpaca_stream import ProbeRecorder, event_lag_ms, main


def test_event_lag_distinguishes_trade_from_minute_bar_close():
    received = datetime(2026, 9, 25, 18, 0, 1, tzinfo=timezone.utc)
    assert event_lag_ms({"t": "2026-09-25T18:00:00Z"}, "trades", received) == 1000
    assert event_lag_ms({"t": "2026-09-25T17:59:00Z"}, "bars", received) == 1000
    assert event_lag_ms({"t": "bad"}, "quotes", received) is None


def test_event_lag_accepts_alpaca_raw_timestamp():
    class Timestamp:
        seconds = 1790359200
        nanoseconds = 500_000_000

    received = datetime.fromtimestamp(1790359201, timezone.utc)
    assert event_lag_ms({"t": Timestamp()}, "quotes", received) == 500


def test_report_keeps_subscription_ack_separate_from_messages():
    recorder = ProbeRecorder({"quotes": ["SPY", "WDC"], "bars": ["SPY", "WDC"]})
    recorder.control({"T": "subscription", "quotes": ["SPY", "WDC"], "bars": ["SPY"]})
    recorder.event({"S": "SPY", "t": "2026-09-25T18:00:00Z"}, "quotes",
                   datetime(2026, 9, 25, 18, 0, 1, tzinfo=timezone.utc))
    report = recorder.report(feed="iex", symbols=["SPY", "WDC"], duration=10)
    assert report["accepted"] == {"quotes": ["SPY", "WDC"], "bars": ["SPY"]}
    assert report["counts"]["quotes"] == {"SPY": 1, "WDC": 0}
    assert report["lag_ms"]["quotes"]["mean"] == 1000


def test_report_exposes_prices_and_volume_for_bar_comparison():
    recorder = ProbeRecorder({"quotes": ["WDC"], "trades": ["WDC"], "bars": ["WDC"]})
    when = datetime(2026, 9, 25, 18, 1, 1, tzinfo=timezone.utc)
    recorder.event({"S": "WDC", "t": "2026-09-25T18:01:00Z", "bp": 92.1,
                    "ap": 92.12, "bs": 10, "as": 20}, "quotes", when)
    recorder.event({"S": "WDC", "t": "2026-09-25T18:01:00Z", "p": 92.11,
                    "s": 3}, "trades", when)
    recorder.event({"S": "WDC", "t": "2026-09-25T18:00:00Z", "o": 92.0,
                    "h": 92.2, "l": 91.9, "c": 92.11, "v": 450, "n": 11,
                    "vw": 92.08}, "bars", when)
    report = recorder.report(feed="iex", symbols=["WDC"], duration=65)
    assert report["latest"]["quotes"]["WDC"]["bid"] == 92.1
    assert report["latest"]["trades"]["WDC"]["price"] == 92.11
    assert report["latest"]["bars"]["WDC"]["close"] == 92.11
    assert report["latest"]["bars"]["WDC"]["volume"] == 450


def test_missing_paper_credentials_never_open_a_stream(monkeypatch, capsys):
    monkeypatch.delenv("alpaca_paper_api_key", raising=False)
    monkeypatch.delenv("alpaca_paper_secret", raising=False)
    assert main(["SPY", "--duration", "5"]) == 2
    assert "no stream opened" in capsys.readouterr().err
