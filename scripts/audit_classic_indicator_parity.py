"""Read-only comparison of current Edge indicators with Pandas TA Classic.

Use a frozen replay input, never a live provider. This audit is diagnostic and
does not select an indicator implementation for alerts.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
import pandas_ta_classic as ta

from scanner.indicators.adx import adx
from scanner.indicators.atr import wilder_atr
from scanner.indicators.ema_sma import ema, sma
from scanner.indicators.vwap import session_vwap
from scanner.replay_input import ReplayInput


def _comparison(current: pd.Series, classic: pd.Series) -> dict:
    left = pd.to_numeric(current, errors="coerce")
    right = pd.to_numeric(classic, errors="coerce")
    common = left.notna() & right.notna()
    difference = (left[common] - right[common]).abs()
    return {
        "current_first_valid": str(left.first_valid_index()) if left.first_valid_index() is not None else None,
        "classic_first_valid": str(right.first_valid_index()) if right.first_valid_index() is not None else None,
        "compared_bars": int(common.sum()),
        "max_abs_difference": float(difference.max()) if len(difference) else None,
        "current_last": float(left.dropna().iloc[-1]) if left.notna().any() else None,
        "classic_last": float(right.dropna().iloc[-1]) if right.notna().any() else None,
    }


def audit(snapshot: ReplayInput, symbol: str) -> dict:
    if symbol not in snapshot.symbols:
        raise ValueError(f"{symbol} is not in this replay input")
    daily = snapshot.frame(symbol, "daily")
    minute = snapshot.frame(symbol, "1min").copy()
    index = pd.DatetimeIndex(minute.index)
    minute.index = (index.tz_localize("UTC") if index.tz is None else index).tz_convert(
        "America/New_York")
    minute = minute.between_time("09:30", "15:59")
    if daily.empty or minute.empty:
        raise ValueError(f"{symbol}: replay input needs prior daily and session one-minute bars")

    classic_adx = ta.adx(daily.high, daily.low, daily.close, length=14, talib=False)
    if classic_adx is None:
        raise ValueError(f"{symbol}: not enough daily bars for Classic ADX(14)")
    pairs = {
        "sma50_daily": (sma(daily.close, 50), ta.sma(daily.close, length=50, talib=False)),
        "ema8_daily": (ema(daily.close, 8), ta.ema(daily.close, length=8, talib=False)),
        "atr14_daily": (
            wilder_atr(daily.high, daily.low, daily.close, 14),
            ta.atr(daily.high, daily.low, daily.close, length=14, talib=False),
        ),
        "adx14_daily": (
            adx(daily.high, daily.low, daily.close, 14), classic_adx["ADX_14"],
        ),
        "vwap_1min_rth": (
            session_vwap(minute.high, minute.low, minute.close, minute.volume),
            ta.vwap(minute.high, minute.low, minute.close, minute.volume, anchor="D"),
        ),
    }
    return {
        "data_id": snapshot.data_id,
        "date": snapshot.date.isoformat(),
        "symbol": symbol,
        "comparisons": {name: _comparison(*pair) for name, pair in pairs.items()},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="Frozen replay input directory")
    parser.add_argument("--symbol", required=True, help="Symbol in that replay input")
    args = parser.parse_args()
    snapshot = ReplayInput.load(args.input)
    print(json.dumps(audit(snapshot, args.symbol.strip().upper()), indent=2))


if __name__ == "__main__":
    main()
