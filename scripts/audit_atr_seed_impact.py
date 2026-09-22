"""Read-only comparison of legacy and canonical ATR against cached market bars.

Run with ``python -m scripts.audit_atr_seed_impact --data-dir data``. This is a
threshold replay, not an alert replay: it does not reproduce one-minute trigger
ordering, universe-profile membership, or alert cooldowns.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import pandas as pd

from scanner.indicators.atr import wilder_atr


def _legacy_atr(bars: pd.DataFrame, length: int) -> pd.Series:
    prev = bars["close"].shift(1)
    tr = pd.concat([
        bars["high"] - bars["low"],
        (bars["high"] - prev).abs(),
        (bars["low"] - prev).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / length, min_periods=length, adjust=False).mean()


def audit(data_dir: Path, sessions: int = 20) -> dict:
    with (data_dir / "universe.csv").open(newline="", encoding="utf-8") as f:
        symbols = [row["symbol"] for row in csv.DictReader(f)]

    result = {
        "symbols": 0, "daily_symbol_days": 0, "intraday_bars": 0,
        "atr_pct_flips": {"1": 0, "2": 0, "4": 0},
        "daily_close_extension_2x_flips": 0,
        "intraday_extension_2x_flips": 0,
        "intraday_break_candidate_flips": 0,
        "max_latest_atr5_delta": 0.0, "max_latest_atr14_delta": 0.0,
        "flipped_symbols": [],
    }
    for symbol in symbols:
        daily_path = data_dir / "schwab" / "daily" / f"{symbol}.parquet"
        if not daily_path.exists():
            continue
        daily = pd.read_parquet(daily_path).sort_index()
        if len(daily) < 20:
            continue
        result["symbols"] += 1
        old5, old14 = _legacy_atr(daily, 5), _legacy_atr(daily, 14)
        new5 = wilder_atr(daily.high, daily.low, daily.close, 5)
        new14 = wilder_atr(daily.high, daily.low, daily.close, 14)
        result["max_latest_atr5_delta"] = max(
            result["max_latest_atr5_delta"], abs(float(new5.iloc[-1] - old5.iloc[-1])))
        result["max_latest_atr14_delta"] = max(
            result["max_latest_atr14_delta"], abs(float(new14.iloc[-1] - old14.iloc[-1])))

        tail = daily.iloc[-sessions:]
        result["daily_symbol_days"] += len(tail)
        prior_close = daily.close.shift(1).loc[tail.index]
        for threshold in (1, 2, 4):
            before = old5.loc[tail.index] / prior_close * 100 >= threshold
            after = new5.loc[tail.index] / prior_close * 100 >= threshold
            result["atr_pct_flips"][str(threshold)] += int((before != after).sum())

        ema8 = daily.close.ewm(span=8, adjust=False).mean()
        old_ext = (tail.close - ema8.shift(1).loc[tail.index]).abs() / old14.shift(1).loc[tail.index]
        new_ext = (tail.close - ema8.shift(1).loc[tail.index]).abs() / new14.shift(1).loc[tail.index]
        result["daily_close_extension_2x_flips"] += int(((old_ext >= 2) != (new_ext >= 2)).sum())

        bars_path = data_dir / "schwab" / "5m" / f"{symbol}.parquet"
        if not bars_path.exists():
            continue
        intraday = pd.read_parquet(bars_path).sort_index()
        bar_dates = pd.Series(intraday.index.tz_convert("America/New_York").date, index=intraday.index)
        daily_dates = pd.Series(daily.index.tz_convert("America/New_York").date, index=daily.index)
        relevant_dates = sorted(set(bar_dates))[-sessions:]
        for day in relevant_dates:
            prior = daily_dates[daily_dates < day].index
            if len(prior) == 0:
                continue
            prior_idx = prior[-1]
            if pd.isna(old14.loc[prior_idx]) or pd.isna(new14.loc[prior_idx]):
                continue
            one_day = intraday.loc[bar_dates == day]
            one_day = (one_day.tz_convert("America/New_York").between_time("09:30", "15:55")
                       if not one_day.empty else one_day)
            if one_day.empty:
                continue
            result["intraday_bars"] += len(one_day)
            before = (one_day.close - ema8.loc[prior_idx]).abs() / old14.loc[prior_idx] >= 2
            after = (one_day.close - ema8.loc[prior_idx]).abs() / new14.loc[prior_idx] >= 2
            flip = before != after
            count = int(flip.sum())
            result["intraday_extension_2x_flips"] += count
            if count:
                result["flipped_symbols"].append(symbol)
            # The shipped extension setup also watches 5-minute new highs/lows.
            # This counts gate changes only on bars with that candidate event.
            candidate = ((one_day.high > one_day.high.shift(1)) |
                         (one_day.low < one_day.low.shift(1)))
            result["intraday_break_candidate_flips"] += int((flip & candidate).sum())
    result["flipped_symbols"] = sorted(set(result["flipped_symbols"]))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--sessions", type=int, default=20)
    args = parser.parse_args()
    for key, value in audit(args.data_dir, args.sessions).items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
