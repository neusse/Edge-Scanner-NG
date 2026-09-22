"""Average Directional Index (ADX)."""

from __future__ import annotations

import math

import pandas as pd


def adx(high: pd.Series, low: pd.Series, close: pd.Series, length: int = 14) -> pd.Series:
    """Wilder ADX on completed OHLC candles.

    The first value needs ``2 * length`` candles: one candle before the first
    directional-movement observation, ``length`` observations to seed the
    smoothed true range and DM values, then ``length`` DX values to seed ADX.
    """
    out = pd.Series(float("nan"), index=close.index, dtype=float)
    n = int(length)
    if n < 2 or len(close) < 2 * n:
        return out

    tr: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for i in range(1, len(close)):
        hi, lo, prev_close = float(high.iloc[i]), float(low.iloc[i]), float(close.iloc[i - 1])
        prev_hi, prev_lo = float(high.iloc[i - 1]), float(low.iloc[i - 1])
        if not all(math.isfinite(x) for x in (hi, lo, prev_close, prev_hi, prev_lo)):
            return out
        up, down = hi - prev_hi, prev_lo - lo
        tr.append(max(hi - lo, abs(hi - prev_close), abs(lo - prev_close)))
        plus_dm.append(up if up > down and up > 0 else 0.0)
        minus_dm.append(down if down > up and down > 0 else 0.0)

    smoothed_tr = sum(tr[:n])
    smoothed_plus = sum(plus_dm[:n])
    smoothed_minus = sum(minus_dm[:n])

    def dx_value() -> float:
        if smoothed_tr <= 0:
            return 0.0
        plus_di = 100.0 * smoothed_plus / smoothed_tr
        minus_di = 100.0 * smoothed_minus / smoothed_tr
        total = plus_di + minus_di
        return 0.0 if total <= 0 else 100.0 * abs(plus_di - minus_di) / total

    dx = [dx_value()]
    for i in range(n, len(tr)):
        smoothed_tr = smoothed_tr - smoothed_tr / n + tr[i]
        smoothed_plus = smoothed_plus - smoothed_plus / n + plus_dm[i]
        smoothed_minus = smoothed_minus - smoothed_minus / n + minus_dm[i]
        dx.append(dx_value())

    if len(dx) < n:
        return out
    value = sum(dx[:n]) / n
    first = 2 * n - 1
    out.iloc[first] = value
    for candle_i, current_dx in enumerate(dx[n:], start=first + 1):
        value = (value * (n - 1) + current_dx) / n
        out.iloc[candle_i] = value
    return out
