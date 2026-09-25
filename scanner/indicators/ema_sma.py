"""Compatibility interface for Classic EMA/SMA calculations."""
from __future__ import annotations

import math

import pandas as pd

from scanner.indicators.classic import calculate


class SeededEMA:
    """Completed-close EMA tracker; each value comes from Pandas TA Classic."""

    __slots__ = ("period", "value", "prev", "_closes")

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("EMA period must be positive")
        self.period = period
        self.value: float | None = None
        self.prev: float | None = None
        self._closes: list[float] = []

    def push(self, close: float) -> float | None:
        price = float(close)
        self.prev = self.value
        if not math.isfinite(price):
            self._closes.clear()
            self.value = self.prev = None
            return None
        self._closes.append(price)
        if len(self._closes) < self.period:
            self.value = None
            return None
        last = calculate(pd.DataFrame({"close": pd.Series(self._closes, dtype=float)}),
                         "ema", self.period)["value"].iloc[-1]
        self.value = None if pd.isna(last) else float(last)
        return self.value

    def seed(self, closes: list[float]) -> pd.Series:
        """Warm from completed closes with one Classic calculation per valid run."""
        prices = pd.Series(closes, dtype=float)
        values = pd.Series(float("nan"), index=prices.index, dtype=float)
        self._closes = []
        self.value = self.prev = None
        start = 0
        for i, price in enumerate(prices):
            if math.isfinite(price):
                continue
            if i - start >= self.period:
                values.iloc[start:i] = calculate(
                    pd.DataFrame({"close": prices.iloc[start:i]}), "ema", self.period
                )["value"].to_numpy()
            start = i + 1
        if len(prices) - start >= self.period:
            values.iloc[start:] = calculate(
                pd.DataFrame({"close": prices.iloc[start:]}), "ema", self.period
            )["value"].to_numpy()
        self._closes = prices.iloc[start:].tolist()
        if len(values):
            last = values.iloc[-1]
            self.value = None if pd.isna(last) else float(last)
        if len(values) > 1:
            previous = values.iloc[-2]
            self.prev = None if pd.isna(previous) else float(previous)
        return values


def ema(close: pd.Series, span: int) -> pd.Series:
    return calculate(pd.DataFrame({"close": close}), "ema", span)["value"]


def sma(close: pd.Series, length: int) -> pd.Series:
    return calculate(pd.DataFrame({"close": close}), "sma", length)["value"]
