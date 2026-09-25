"""Offline replay feed and timestamp-batched playback controller."""
from __future__ import annotations

import threading
from datetime import date
from typing import Callable

import pandas as pd

from scanner.data.interface import DataFeed
from scanner.replay_input import ReplayInput


class ReplayFeed(DataFeed):
    """Serve only frozen input frames; network and streaming are impossible."""

    per_symbol_history = True

    def __init__(self, snapshot: ReplayInput) -> None:
        self.snapshot = snapshot

    def get_historical_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        return self.snapshot.frame(symbol, "daily")

    def get_historical_bars(self, symbol: str, timeframe: str, start: date, end: date) -> pd.DataFrame:
        if timeframe != "5Min":
            raise ValueError("replay snapshot only holds 5-minute historical context")
        return self.snapshot.frame(symbol, "5min")

    def get_bars_range(self, symbol: str, timeframe: str, start: date, end: date) -> pd.DataFrame:
        raise RuntimeError("replay dashboard cannot fetch unrecorded or future bars")

    def subscribe_minute_bars(self, symbols: list[str], callback: Callable[[dict], None]) -> None:
        raise RuntimeError("replay cannot open a market-data stream")

    def get_snapshot(self, symbols: list[str]) -> dict[str, dict]:
        return {}


class ReplayController:
    """Process all symbols at one market timestamp before advancing time."""

    def __init__(self, bars: list[dict], on_group: Callable[[list[dict]], None],
                 on_reset: Callable[[], None], *, on_complete: Callable[[], None] | None = None,
                 speed: float = 60.0,
                 paused: bool = False) -> None:
        if speed < 0:
            raise ValueError("speed must be non-negative (0 means fastest possible)")
        self.groups: list[list[dict]] = []
        for bar in bars:
            if not self.groups or self.groups[-1][0]["timestamp"] != bar["timestamp"]:
                self.groups.append([])
            self.groups[-1].append(bar)
        self.on_group = on_group
        self.on_reset = on_reset
        self.on_complete = on_complete
        self._cv = threading.Condition()
        self._index = 0
        self._paused = paused
        self._steps = 0
        self._stopped = False
        self._reset = False
        self._resetting = False
        self._speed = speed

    def status(self) -> dict:
        with self._cv:
            cursor = (self.groups[self._index - 1][0]["timestamp"].isoformat()
                      if self._index else None)
            return {"position": self._index, "total": len(self.groups), "cursor": cursor,
                    "paused": self._paused, "complete": self._index >= len(self.groups),
                    "speed": self._speed}

    def control(self, action: str, *, speed: float | None = None) -> dict:
        with self._cv:
            if action == "pause":
                self._paused = True
            elif action == "resume":
                self._paused = False
                self._steps = 0
            elif action == "step":
                self._paused = True
                self._steps += 1
            elif action == "reset":
                self._reset = True
                self._resetting = True
            elif action == "stop":
                self._stopped = True
            elif action == "speed":
                if speed is None or speed < 0:
                    raise ValueError("speed must be non-negative")
                self._speed = speed
            else:
                raise ValueError(f"unknown replay action: {action}")
            self._cv.notify_all()
            if action == "reset":
                while self._resetting and not self._stopped:
                    self._cv.wait(timeout=5)
        return self.status()

    def run(self) -> None:
        while True:
            with self._cv:
                while not self._stopped and not self._reset and (
                    self._index >= len(self.groups) or (self._paused and self._steps == 0)
                ):
                    self._cv.wait()
                if self._stopped:
                    return
                if self._reset:
                    self._reset = False
                    self._index = 0
                    self._steps = 0
                    self._paused = True
                    reset = True
                else:
                    reset = False
                    group = self.groups[self._index]
                    previous = self.groups[self._index - 1] if self._index else None
                    speed = self._speed
                    if self._steps:
                        self._steps -= 1
            if reset:
                try:
                    self.on_reset()
                finally:
                    with self._cv:
                        self._resetting = False
                        self._cv.notify_all()
                continue
            if previous and speed > 0 and not self._paused:
                delta = (group[0]["timestamp"] - previous[0]["timestamp"]).total_seconds()
                if delta > 0:
                    with self._cv:
                        self._cv.wait(timeout=min(delta / speed, 60.0))
                        if self._stopped or self._reset or self._paused:
                            continue
            self.on_group(group)
            with self._cv:
                self._index += 1
                complete = self._index >= len(self.groups)
            if complete and self.on_complete is not None:
                self.on_complete()
