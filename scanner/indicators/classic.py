"""Pandas TA Classic is the sole calculator for native technical studies.

Callers provide only completed OHLCV bars. Values are indexed by those bars;
missing warm-up values remain NaN and must never be treated as a signal.
"""
from __future__ import annotations

import logging
import pandas as pd
import pandas_ta_classic as ta
import warnings

VERSION = f"pandas-ta-classic/{ta.version}"
log = logging.getLogger(__name__)


def _empty(index: pd.Index, columns: tuple[str, ...]) -> pd.DataFrame:
    return pd.DataFrame(float("nan"), index=index, columns=list(columns))


def _chronological(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.Index | None]:
    """Return candles in calculation order and the caller order to restore.

    Native studies are order-dependent. Provider responses and replay fixtures
    are allowed to arrive out of order, but two candles at the same timestamp
    are ambiguous and must be reconciled by the caller before calculation.
    """
    if not isinstance(frame.index, pd.DatetimeIndex):
        return frame, None
    if frame.index.hasnans:
        raise ValueError("indicator candles require valid timestamps")
    if frame.index.has_duplicates:
        raise ValueError("indicator candles require unique timestamps")
    original = frame.index.copy()
    if frame.index.tz is None:
        log.warning("Indicator timestamps had no timezone; interpreting them as UTC")
        frame = frame.copy()
        frame.index = frame.index.tz_localize("UTC")
        if not frame.index.is_monotonic_increasing:
            log.warning("Indicator candles arrived out of order; sorting by timestamp")
            frame = frame.sort_index(kind="stable")
        return frame, original
    if frame.index.is_monotonic_increasing:
        return frame, None
    log.warning("Indicator candles arrived out of order; sorting by timestamp")
    return frame.sort_index(kind="stable"), original


def _restore_order(result: pd.DataFrame, original: pd.Index | None) -> pd.DataFrame:
    if original is None:
        return result
    lookup = original
    if isinstance(original, pd.DatetimeIndex) and original.tz is None:
        lookup = original.tz_localize("UTC")
    restored = result.reindex(lookup)
    restored.index = original
    return restored


def calculate(frame: pd.DataFrame, name: str, length: int | None = None) -> pd.DataFrame:
    """Return canonical named columns for one native study on completed bars.

    Native lengths are configurable for SMA/EMA/ATR/ADX/RSI/CCI; MACD,
    stochastic and Bollinger use their standard 12/26/9, 14/3/3 and 20/2
    definitions. OBV and VWAP have no length. VWAP resets on each ET date.
    """
    name = name.lower()
    columns = {
        "sma": ("value",), "ema": ("value",), "atr": ("value",),
        "adx": ("value", "plus_di", "minus_di"),
        "rsi": ("value",), "cci": ("value",), "obv": ("value",),
        "macd": ("value", "signal", "histogram"),
        "stoch": ("k", "d"),
        "bbands": ("lower", "middle", "upper", "bandwidth", "percent_b"),
        "vwap": ("value",),
    }.get(name)
    if columns is None:
        raise ValueError(f"unknown Classic indicator: {name}")
    if frame.empty:
        return _empty(frame.index, columns)
    frame, original_order = _chronological(frame)
    # A missing candle is a discontinuity, not a zero-range observation.
    # Restart native studies on each complete segment; the arithmetic remains
    # Classic's, while Edge owns input integrity and session selection.
    needed = ["close"]
    if name in {"atr", "adx", "cci", "stoch", "vwap"}:
        needed += ["high", "low"]
    if name in {"obv", "vwap"}:
        needed.append("volume")
    complete = frame[needed].notna().all(axis=1)
    if not complete.all():
        result = _empty(frame.index, columns)
        segment = (complete != complete.shift(fill_value=False)).cumsum()
        for _, part in frame[complete].groupby(segment[complete]):
            result.loc[part.index] = calculate(part, name, length).to_numpy()
        return _restore_order(result, original_order)
    close = pd.to_numeric(frame["close"], errors="coerce")
    high = pd.to_numeric(frame["high"], errors="coerce") if "high" in frame else None
    low = pd.to_numeric(frame["low"], errors="coerce") if "low" in frame else None
    volume = pd.to_numeric(frame["volume"], errors="coerce") if "volume" in frame else None
    n = int(length or {"sma": 50, "ema": 9, "atr": 14, "adx": 14,
                       "rsi": 14, "cci": 14}.get(name, 0))
    if name in {"sma", "ema", "atr", "adx", "rsi", "cci"} and n < 1:
        raise ValueError("indicator length must be positive")
    if name == "sma":
        raw = ta.sma(close, length=n, talib=False)
    elif name == "ema":
        raw = ta.ema(close, length=n, talib=False)
    elif name == "atr":
        raw = ta.atr(high, low, close, length=n, talib=False)
    elif name == "adx":
        raw = ta.adx(high, low, close, length=n, talib=False)
    elif name == "rsi":
        raw = ta.rsi(close, length=n, talib=False)
    elif name == "cci":
        raw = ta.cci(high, low, close, length=n, talib=False)
    elif name == "obv":
        raw = ta.obv(close, volume, talib=False)
    elif name == "macd":
        raw = ta.macd(close, talib=False)
    elif name == "stoch":
        raw = ta.stoch(high, low, close, talib=False)
    elif name == "bbands":
        raw = ta.bbands(close, length=20, std=2.0, talib=False)
    else:
        if not isinstance(frame.index, pd.DatetimeIndex):
            raise ValueError("VWAP requires timestamped candles")
        index = frame.index.tz_localize("UTC") if frame.index.tz is None else frame.index
        et = frame.copy()
        et.index = index.tz_convert("America/New_York")
        package_log = logging.getLogger("pandas_ta_classic.overlap.vwap")

        class _SinglePointOrderFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                return not (len(et) == 1 and "not datetime ordered" in record.getMessage())

        false_positive_filter = _SinglePointOrderFilter()
        package_log.addFilter(false_positive_filter)
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Converting to PeriodArray/Index representation will drop timezone information")
            try:
                raw = ta.vwap(et.high, et.low, et.close, et.volume, anchor="D")
            finally:
                package_log.removeFilter(false_positive_filter)
        if raw is not None:
            raw.index = frame.index
    if raw is None:
        return _restore_order(_empty(frame.index, columns), original_order)
    if isinstance(raw, pd.Series):
        result = pd.DataFrame({columns[0]: raw}, index=frame.index)
        return _restore_order(result, original_order)
    # Classic output order is documented and fixed by the pinned version.
    if name == "macd":
        raw = raw.iloc[:, [0, 2, 1]]
    result = pd.DataFrame(raw.to_numpy(), index=frame.index, columns=list(columns))
    return _restore_order(result, original_order)


def candles_frame(candles: list[dict]) -> pd.DataFrame:
    """Build a timestamped frame from completed SymbolSeries candles."""
    if not candles:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    rows = []
    timestamps = []
    for i, candle in enumerate(candles):
        key = candle.get("key")
        if key and isinstance(key[0], str):
            # Candle's key is (ET date, session, slot); its exact timestamp is
            # not needed except for VWAP's ET-date reset.
            stamp = pd.Timestamp(key[0], tz="America/New_York") + pd.Timedelta(minutes=i % 390)
        else:
            stamp = pd.Timestamp("2000-01-01", tz="America/New_York") + pd.Timedelta(minutes=i)
        timestamps.append(stamp)
        rows.append({k: candle.get(k, 0.0) for k in ("open", "high", "low", "close", "volume")})
    return pd.DataFrame(rows, index=pd.DatetimeIndex(timestamps))
