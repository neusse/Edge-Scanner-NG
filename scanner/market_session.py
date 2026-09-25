"""NYSE session boundaries used by live shutdown and history refresh."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Callable

import pandas as pd
import pandas_market_calendars as mcal

_NYSE = mcal.get_calendar("NYSE")


def session_close(day: date) -> datetime | None:
    """Official close in UTC, including early closes; None on closed days."""
    schedule = _NYSE.schedule(start_date=day, end_date=day)
    if schedule.empty:
        return None
    return pd.Timestamp(schedule.iloc[0]["market_close"]).to_pydatetime().astimezone(timezone.utc)


def past_session_close(now: datetime, *, grace: timedelta = timedelta()) -> bool:
    """Whether today's NYSE session ended at least `grace` ago."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    from zoneinfo import ZoneInfo
    close = session_close(now.astimezone(ZoneInfo("America/New_York")).date())
    return close is None or now.astimezone(timezone.utc) >= close + grace


def stop_stream_at_close(close: datetime, stop: Callable[[], None], cancelled,
                         *, now: datetime | None = None,
                         grace: timedelta = timedelta(seconds=90)) -> bool:
    """Wait for the final bar, then stop from outside the stream thread.

    Returns False if the scan ended first. The caller owns this daemon thread.
    """
    current = now or datetime.now(timezone.utc)
    remaining = max(0.0, ((close + grace) - current).total_seconds())
    if cancelled.wait(remaining):
        return False
    stop()
    return True
