import pandas as pd

from scanner.indicators.classic import calculate


def session_vwap(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    volume: pd.Series,
) -> pd.Series:
    """Running session VWAP computed from bar-level data.

    Typical price = (high + low + close) / 3
    VWAP = cumsum(tp * volume) / cumsum(volume)

    The index must span a single trading session — call cumsum from bar 0.
    Resets are handled externally by slicing per-session before calling.
    """
    index = high.index
    if not isinstance(index, pd.DatetimeIndex):
        # Compatibility for callers supplying a single-session RangeIndex.
        index = pd.date_range("2000-01-03 09:30", periods=len(high), freq="min",
                              tz="America/New_York")
    frame = pd.DataFrame({"high": high.to_numpy(), "low": low.to_numpy(),
                          "close": close.to_numpy(), "volume": volume.to_numpy()}, index=index)
    out = calculate(frame, "vwap")["value"]
    out.index = high.index
    return out
