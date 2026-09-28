"""Lossless handoff from an early market-data stream to a warmed scanner."""
from __future__ import annotations

from collections import deque
from threading import Event, RLock, Thread
from typing import Callable, Mapping

import pandas as pd


BarCallback = Callable[[dict], None]


def _stamp(value) -> pd.Timestamp:
    stamp = pd.Timestamp(value)
    return stamp.tz_localize("UTC") if stamp.tzinfo is None else stamp.tz_convert("UTC")


class StartupBarBuffer:
    """Buffer bars until cached state is ready, then atomically go live.

    Incoming callbacks never run scanner code while startup is mutating state.
    ``activate`` drains every queued batch, including events received during the
    drain, before swapping the callback to the live evaluator.
    """

    def __init__(self, max_events: int = 100_000,
                 priority: Callable[[dict], int] | None = None) -> None:
        if max_events < 1:
            raise ValueError("max_events must be positive")
        self._events: deque[dict] = deque()
        self._max_events = max_events
        self._priority = priority or (lambda _bar: 0)
        self._lock = RLock()
        self._live: BarCallback | None = None
        self._dropped = 0
        self._phase = "buffering"

    def snapshot(self) -> dict:
        """Thread-safe startup diagnostics for the feed status endpoint."""
        with self._lock:
            oldest = min(
                (_stamp(bar["timestamp"]) for bar in self._events), default=None
            )
            return {
                "phase": self._phase,
                "ready": self._live is not None,
                "buffered_events": len(self._events),
                "oldest_buffered_timestamp": oldest.isoformat() if oldest is not None else None,
                "dropped_events": self._dropped,
            }

    def __call__(self, bar: dict) -> None:
        callback: BarCallback | None
        with self._lock:
            callback = self._live
            if callback is None:
                if len(self._events) >= self._max_events:
                    self._events.popleft()
                    self._dropped += 1
                self._events.append(dict(bar))
                return
        callback(bar)

    def activate(self, prime: BarCallback, live: BarCallback,
                 cutoffs: Mapping[str, object] | None = None,
                 ready: Callable[[], None] | None = None) -> dict[str, int]:
        """Prime queued bars newer than cache cutoffs, then route directly live."""
        normalized_cutoffs = {
            str(symbol).upper(): _stamp(value) for symbol, value in (cutoffs or {}).items()
        }
        primed = overlap = 0
        applied = dict(normalized_cutoffs)
        with self._lock:
            self._phase = "draining"
        while True:
            with self._lock:
                if self._dropped:
                    self._phase = "failed"
                    raise RuntimeError(
                        f"startup stream buffer overflowed; {self._dropped} event(s) were lost"
                    )
                batch = list(self._events)
                self._events.clear()
                if not batch:
                    if ready is not None:
                        ready()
                    self._live = live
                    self._phase = "live"
                    return {"primed": primed, "overlap": overlap, "dropped": 0}
            batch.sort(key=lambda bar: (
                _stamp(bar["timestamp"]), self._priority(bar), str(bar.get("symbol") or "")
            ))
            for bar in batch:
                symbol = str(bar.get("symbol") or "").upper()
                stamp = _stamp(bar["timestamp"])
                if symbol in applied and stamp <= applied[symbol]:
                    overlap += 1
                    continue
                prime(bar)
                applied[symbol] = stamp
                primed += 1


class EarlyStreamSession:
    """Own one provider subscription running beside startup warmup."""

    def __init__(self, feed, symbols: list[str], callback: BarCallback) -> None:
        self.feed = feed
        self.symbols = list(symbols)
        self.callback = callback
        self.started = Event()
        self._thread: Thread | None = None
        self._error: BaseException | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("early stream session already started")

        def _run() -> None:
            self.started.set()
            try:
                self.feed.subscribe_minute_bars(self.symbols, self.callback)
            except BaseException as exc:  # surfaced synchronously by wait/check
                self._error = exc

        self._thread = Thread(target=_run, daemon=True, name="early-market-stream")
        self._thread.start()

    def check(self) -> None:
        if self._error is not None:
            raise self._error

    def wait(self) -> None:
        if self._thread is None:
            raise RuntimeError("early stream session was not started")
        while self._thread.is_alive():
            self._thread.join(1.0)
            self.check()
        self.check()
