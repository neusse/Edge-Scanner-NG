import math

import pandas as pd


def wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, length: int) -> pd.Series:
    """Wilder's Average True Range.

    True Range = max(high-low, |high-prev_close|, |low-prev_close|)
    Seeded with the arithmetic mean of the first ``length`` true ranges, then
    updated with Wilder's recurrence: (prior ATR * (length - 1) + TR) / length.
    A missing/invalid candle resets the warm-up rather than crossing a data gap.
    """
    if length < 1:
        raise ValueError("ATR length must be positive")
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()],
        axis=1,
    ).max(axis=1).where(high.notna() & low.notna() & close.notna())
    out = pd.Series(float("nan"), index=tr.index, dtype=float)
    warmup: list[float] = []
    value: float | None = None
    for i, raw in enumerate(tr):
        current = float(raw)
        if not math.isfinite(current):
            warmup.clear()
            value = None
            continue
        if value is None:
            warmup.append(current)
            if len(warmup) == length:
                value = sum(warmup) / length
                out.iloc[i] = value
        else:
            value = (value * (length - 1) + current) / length
            out.iloc[i] = value
    return out
