"""Startup stream buffering at the public callback boundary."""
from __future__ import annotations

import pandas as pd
import threading

from scanner.startup_stream import EarlyStreamSession, StartupBarBuffer


def _bar(symbol: str, minute: int) -> dict:
    return {
        "symbol": symbol,
        "timestamp": pd.Timestamp(f"2026-09-25 09:{minute:02d}", tz="America/New_York"),
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.5,
        "volume": 1_000.0,
    }


def test_startup_buffer_reconciles_overlap_then_switches_to_live_without_loss():
    buffer = StartupBarBuffer(max_events=20)
    primed: list[tuple[str, int]] = []
    live: list[tuple[str, int]] = []
    buffer(_bar("AAPL", 32))
    buffer(_bar("AAPL", 31))

    def prime(bar: dict) -> None:
        primed.append((bar["symbol"], bar["timestamp"].minute))
        if bar["timestamp"].minute == 32:
            # Simulate the stream callback racing with reconciliation.
            buffer(_bar("AAPL", 33))

    stats = buffer.activate(
        prime,
        lambda bar: live.append((bar["symbol"], bar["timestamp"].minute)),
        cutoffs={"AAPL": _bar("AAPL", 31)["timestamp"]},
    )
    buffer(_bar("AAPL", 34))

    assert primed == [("AAPL", 32), ("AAPL", 33)]
    assert live == [("AAPL", 34)]
    assert stats == {"primed": 2, "overlap": 1, "dropped": 0}
    assert buffer.snapshot() == {
        "phase": "live", "ready": True, "buffered_events": 0,
        "oldest_buffered_timestamp": None, "dropped_events": 0,
    }


def test_startup_buffer_deduplicates_stream_reconnect_snapshot_and_arms_atomically():
    buffer = StartupBarBuffer(max_events=20)
    primed = []
    order = []
    same = _bar("AAPL", 32)
    buffer(same)
    buffer(dict(same))

    stats = buffer.activate(
        lambda bar: primed.append(bar),
        lambda _bar: order.append("live"),
        ready=lambda: order.append("ready"),
    )
    buffer(_bar("AAPL", 33))

    assert len(primed) == 1
    assert stats["overlap"] == 1
    assert order == ["ready", "live"]


def test_startup_buffer_overflow_fails_closed_with_diagnostics():
    buffer = StartupBarBuffer(max_events=1)
    buffer(_bar("AAPL", 30))
    buffer(_bar("AAPL", 31))

    try:
        buffer.activate(lambda _bar: None, lambda _bar: None)
        raise AssertionError("overflow was silently accepted")
    except RuntimeError as exc:
        assert "1 event(s) were lost" in str(exc)

    status = buffer.snapshot()
    assert status["phase"] == "failed"
    assert status["ready"] is False
    assert status["dropped_events"] == 1


def test_early_stream_session_opens_once_and_surfaces_background_failure():
    release = threading.Event()

    class Feed:
        calls = 0

        def subscribe_minute_bars(self, symbols, callback):
            self.calls += 1
            callback(_bar(symbols[-1], 30))
            release.wait(1.0)
            raise RuntimeError("stream ended unexpectedly")

    received = []
    feed = Feed()
    session = EarlyStreamSession(feed, ["SPY", "AAPL"], received.append)
    session.start()
    assert session.started.wait(1.0)
    release.set()

    try:
        session.wait()
        raise AssertionError("background stream failure was hidden")
    except RuntimeError as exc:
        assert str(exc) == "stream ended unexpectedly"
    assert feed.calls == 1
    assert received[0]["symbol"] == "AAPL"
