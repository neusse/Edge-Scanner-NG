"""History requests are based on bar coverage, not cache-file write times."""
from datetime import date

import pandas as pd

from scanner.cache.history import merge_history, plan_history


def _bars(*days: str) -> pd.DataFrame:
    idx = pd.DatetimeIndex([pd.Timestamp(f"{day} 15:00", tz="UTC") for day in days])
    return pd.DataFrame({"open": [10.] * len(idx), "high": [11.] * len(idx),
                         "low": [9.] * len(idx), "close": [10.] * len(idx),
                         "volume": [100.] * len(idx)}, index=idx)


def test_complete_cache_needs_no_request_even_after_weekend_or_nyse_holiday():
    cached = _bars("2026-07-02")  # Friday July 3 observes Independence Day
    assert plan_history(cached, date(2026, 7, 2), date(2026, 7, 3)).mode == "current"
    assert plan_history(cached, date(2026, 7, 2), date(2026, 7, 5)).mode == "current"


def test_only_missing_tail_or_front_is_requested():
    cached = _bars("2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08")
    assert plan_history(cached, date(2026, 1, 5), date(2026, 1, 9)).spans == (
        (date(2026, 1, 8), date(2026, 1, 9)),)
    later = _bars("2026-01-07", "2026-01-08", "2026-01-09")
    assert plan_history(later, date(2026, 1, 5), date(2026, 1, 9)).spans == (
        (date(2026, 1, 5), date(2026, 1, 7)),)


def test_sparse_symbol_recorded_as_checked_does_not_refetch_same_window():
    cached = _bars("2026-01-05")
    assert plan_history(cached, date(2026, 1, 5), date(2026, 1, 9),
                        checked_end=date(2026, 1, 9)).mode == "current"


def test_missing_malformed_or_forced_cache_gets_full_window():
    start, end = date(2026, 1, 5), date(2026, 1, 9)
    assert plan_history(None, start, end).spans == ((start, end),)
    broken = _bars("2026-01-05", "2026-01-05")
    assert plan_history(broken, start, end).mode == "full"
    bad_volume = _bars("2026-01-05")
    bad_volume.iloc[0, bad_volume.columns.get_loc("volume")] = float("nan")
    assert plan_history(bad_volume, start, end).mode == "full"
    assert plan_history(_bars("2026-01-05"), start, end, force=True).spans == ((start, end),)


def test_merge_replaces_overlap_without_duplicate_timestamps():
    old = _bars("2026-01-05", "2026-01-06")
    fresh = _bars("2026-01-06", "2026-01-07")
    fresh.iloc[0, fresh.columns.get_loc("close")] = 12.
    merged = merge_history(old, fresh)
    assert len(merged) == 3 and merged.index.is_unique
    assert merged.loc[fresh.index[0], "close"] == 12.
