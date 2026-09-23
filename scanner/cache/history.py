"""Plan and merge historical-bar cache updates without trusting file timestamps."""
from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd
import pandas_market_calendars as mcal

_ET = "America/New_York"
_NYSE = mcal.get_calendar("NYSE")
_REQUIRED = ("open", "high", "low", "close", "volume")


@lru_cache(maxsize=512)
def last_session(day: date) -> date:
    days = _NYSE.valid_days(day - timedelta(days=14), day)
    if days.empty:
        raise ValueError(f"No NYSE session on or before {day}")
    return days[-1].date()


@lru_cache(maxsize=512)
def first_session(day: date) -> date:
    days = _NYSE.valid_days(day, day + timedelta(days=14))
    if days.empty:
        raise ValueError(f"No NYSE session on or after {day}")
    return days[0].date()


def session_bounds(df: pd.DataFrame | None) -> tuple[date, date] | None:
    """Reject corrupt/incomplete cache files before they can suppress a fetch."""
    if df is None or df.empty or not isinstance(df.index, pd.DatetimeIndex):
        return None
    if df.index.tz is None or df.index.hasnans or not df.index.is_monotonic_increasing or not df.index.is_unique:
        return None
    if any(col not in df.columns for col in _REQUIRED):
        return None
    values = df.loc[:, _REQUIRED]
    try:
        if not np.isfinite(values.to_numpy(dtype=float)).all():
            return None
        if (values["volume"] < 0).any():
            return None
    except (TypeError, ValueError):
        return None
    days = df.index.tz_convert(_ET).date
    return days[0], days[-1]


@dataclass(frozen=True)
class HistoryPlan:
    mode: str  # current, incremental, full
    spans: tuple[tuple[date, date], ...]


def plan_history(df: pd.DataFrame | None, start: date, end: date, *,
                 covered_start: bool = False, checked_end: date | None = None,
                 force: bool = False) -> HistoryPlan:
    if force:
        return HistoryPlan("full", ((start, end),))
    bounds = session_bounds(df)
    if bounds is None:
        return HistoryPlan("full", ((start, end),))
    first, last = bounds
    needed_first, needed_last = first_session(start), last_session(end)
    spans: list[tuple[date, date]] = []
    # Overlap the boundary session so a corrected or interrupted final bar is
    # replaced by the provider's answer. Sparse new listings are tracked by
    # covered_start / checked_end instead of being re-fetched on every launch.
    if first > needed_first and not covered_start:
        spans.append((start, first))
    if last < needed_last and (checked_end is None or checked_end < needed_last):
        tail_start = max(last, checked_end) if checked_end else last
        spans.append((tail_start, end))
    return HistoryPlan("incremental", tuple(spans)) if spans else HistoryPlan("current", ())


def merge_history(*frames: pd.DataFrame) -> pd.DataFrame:
    """Later provider responses win on overlapping timestamps."""
    valid = [df for df in frames if df is not None and not df.empty]
    if not valid:
        return pd.DataFrame()
    merged = pd.concat(valid).sort_index(kind="stable")
    return merged.loc[~merged.index.duplicated(keep="last")]


class RequestedCoverage:
    """Remember successfully requested bounds for sparse/recently listed symbols.

    Older Schwab ``_asked.json`` files contain a start-date string per symbol;
    the two-bound format reads those without discarding the prior coverage.
    """

    def __init__(self, cache_dir: Path) -> None:
        self._path = Path(cache_dir) / "_asked.json"
        self._lock = threading.Lock()
        self._dirty = False
        try:
            loaded = json.loads(self._path.read_text(encoding="utf-8"))
            self._map: dict[str, dict | str] = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            self._map = {}

    def covers(self, symbol: str, start: date) -> bool:
        got = self._map.get(symbol)
        earliest = got.get("start") if isinstance(got, dict) else got
        return isinstance(earliest, str) and earliest <= start.isoformat()

    def checked_through(self, symbol: str) -> date | None:
        got = self._map.get(symbol)
        value = got.get("end") if isinstance(got, dict) else None
        try:
            return date.fromisoformat(value) if value else None
        except (TypeError, ValueError):
            return None

    def note(self, symbol: str, start: date, end: date) -> None:
        with self._lock:
            old = self._map.get(symbol)
            prior_start = old.get("start") if isinstance(old, dict) else old
            prior_end = old.get("end") if isinstance(old, dict) else None
            prior_start = prior_start if isinstance(prior_start, str) else None
            prior_end = prior_end if isinstance(prior_end, str) else None
            record = {"start": min(prior_start or start.isoformat(), start.isoformat()),
                      "end": max(prior_end or end.isoformat(), end.isoformat())}
            if old != record:
                self._map[symbol] = record
                self._dirty = True

    def save(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self._path.with_suffix(".tmp")
                tmp.write_text(json.dumps(self._map), encoding="utf-8")
                os.replace(tmp, self._path)
                self._dirty = False
            except OSError:
                # The cache remains usable; this marker is only an optimization.
                pass
