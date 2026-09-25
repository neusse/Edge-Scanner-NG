"""Session cutoffs include holidays and scheduled early closes."""
from datetime import date, datetime, timedelta, timezone

import threading

from scanner.market_session import past_session_close, session_close, stop_stream_at_close


def test_regular_session_close_is_1600_et():
    assert session_close(date(2026, 9, 24)) == datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
    assert not past_session_close(datetime(2026, 9, 24, 19, 59, tzinfo=timezone.utc))
    assert past_session_close(datetime(2026, 9, 24, 20, 2, tzinfo=timezone.utc),
                              grace=timedelta(seconds=90))


def test_early_close_and_holiday():
    assert session_close(date(2026, 11, 27)) == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    assert session_close(date(2026, 12, 25)) is None


def test_close_watcher_stops_once_or_cancels_cleanly():
    close = datetime(2026, 9, 24, 20, 0, tzinfo=timezone.utc)
    calls = []
    cancelled = threading.Event()
    assert stop_stream_at_close(close, lambda: calls.append("stop"), cancelled,
                                now=close + timedelta(minutes=2))
    assert calls == ["stop"]
    cancelled.set()
    assert not stop_stream_at_close(close, lambda: calls.append("again"), cancelled,
                                    now=close + timedelta(minutes=2))
    assert calls == ["stop"]
