import math

import pandas as pd


class SeededEMA:
    """EMA over completed closes, available after an SMA-sized warm-up."""

    __slots__ = ("period", "value", "prev", "_seed")

    def __init__(self, period: int) -> None:
        if period < 1:
            raise ValueError("EMA period must be positive")
        self.period = period
        self.value: float | None = None
        self.prev: float | None = None
        self._seed: list[float] = []

    def push(self, close: float) -> float | None:
        price = float(close)
        self.prev = self.value
        if not math.isfinite(price):
            self.value = None
            self.prev = None
            self._seed.clear()
        elif self.value is None:
            self._seed.append(price)
            if len(self._seed) == self.period:
                self.value = sum(self._seed) / self.period
                self._seed.clear()
        else:
            self.value += (2.0 / (self.period + 1)) * (price - self.value)
        return self.value


def ema(close: pd.Series, span: int) -> pd.Series:
    """SMA-seeded EMA over completed prices, with NaN during warm-up."""
    tracker = SeededEMA(span)
    return pd.Series([tracker.push(price) for price in close], index=close.index,
                     dtype=float)


def sma(close: pd.Series, length: int) -> pd.Series:
    """Simple moving average over `length` periods."""
    return close.rolling(length).mean()


def ema_update(prev: float | None, price: float, span: int) -> float:
    """Incremental EMA update for live bar-by-bar processing.

    Seeds from `price` if `prev` is None (first bar of session).
    """
    if prev is None:
        return price
    alpha = 2.0 / (span + 1)
    return alpha * price + (1.0 - alpha) * prev
