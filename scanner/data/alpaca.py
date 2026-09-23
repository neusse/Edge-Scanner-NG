import logging
import os
import random
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

from alpaca.data.enums import Adjustment, DataFeed as AlpacaDataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

from scanner.cache import parquet
from scanner.cache.history import HistoryPlan, RequestedCoverage, merge_history, plan_history
from scanner.data.interface import DataFeed, Timeframe

load_dotenv(override=True)

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

def market_data_feed() -> AlpacaDataFeed:
    """Which Alpaca data feed to use: ALPACA_FEED=sip (default) or iex.

    SIP is the consolidated tape and needs a paid market-data subscription.
    IEX is free but covers one exchange, a few percent of volume, so volume,
    RVOL and anything built on them read far lower than on SIP. Every default
    threshold was set on SIP data.
    """
    return AlpacaDataFeed.IEX if os.environ.get("ALPACA_FEED", "sip").strip().lower() == "iex" else AlpacaDataFeed.SIP


# History is SPLIT-ADJUSTED (see _ADJUST). The cache folders carry the
# adjustment in their name: the old data/daily and data/5m hold raw bars, and a
# cache that mixed raw and adjusted files would be wrong in both directions.
_DEFAULT_DAILY_CACHE = Path("data/daily_split")
_DEFAULT_INTRADAY_CACHE = Path("data/5m_split")

# Alpaca returns RAW bars unless asked. Across a split, raw history mixes old
# and new share terms: after a 1-for-80 reverse split the 20-day volume reads
# 80x too high and every moving average, 60-day level and ATR spans a price
# jump that never happened. Split adjustment matches what charts show.
# Dividends are left unadjusted, also as charts show by default.
_ADJUST = Adjustment.SPLIT

_BAR_COLS = ["open", "high", "low", "close", "volume", "vwap", "trade_count"]

_ALPACA_TIMEFRAME: dict[Timeframe, TimeFrame] = {
    "1Min":  TimeFrame(1,  TimeFrameUnit.Minute),
    "5Min":  TimeFrame(5,  TimeFrameUnit.Minute),
    "15Min": TimeFrame(15, TimeFrameUnit.Minute),
    "30Min": TimeFrame(30, TimeFrameUnit.Minute),
    "1Hour": TimeFrame(1,  TimeFrameUnit.Hour),
    "4Hour": TimeFrame(4,  TimeFrameUnit.Hour),
    "1Week": TimeFrame(1,  TimeFrameUnit.Week),
    "Day":   TimeFrame.Day,
}


class AlpacaFeed(DataFeed):
    """DataFeed backed by Alpaca Markets (alpaca-py SDK; SIP or IEX, see market_data_feed)."""

    def __init__(
        self,
        *,  # keyword-only: a positional AlpacaFeed(key, secret) creates cache dirs named after the credentials
        cache_dir: Path = _DEFAULT_DAILY_CACHE,
        intraday_cache_dir: Path = _DEFAULT_INTRADAY_CACHE,
        force_refresh_history: bool = False,
    ) -> None:
        api_key = os.environ["ALPACA_API_KEY"]
        secret_key = os.environ["ALPACA_SECRET_KEY"]
        self._client = StockHistoricalDataClient(api_key, secret_key)
        self._cache_dir = Path(cache_dir)
        self._intraday_cache_dir = Path(intraday_cache_dir)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._intraday_cache_dir.mkdir(parents=True, exist_ok=True)
        self.force_refresh_history = force_refresh_history
        self._asked_daily = RequestedCoverage(self._cache_dir)
        self._asked_intraday = RequestedCoverage(self._intraday_cache_dir)
        self._history_stats = {"daily": {"current": 0, "incremental": 0, "full": 0, "requests": 0},
                               "5m": {"current": 0, "incremental": 0, "full": 0, "requests": 0}}

    def _history_context(self, cache_dir: Path) -> tuple[RequestedCoverage, str]:
        return (self._asked_daily, "daily") if cache_dir == self._cache_dir else (self._asked_intraday, "5m")

    def _load_plan(self, symbol: str, start: date, end: date,
                   cache_dir: Path) -> tuple[pd.DataFrame | None, HistoryPlan]:
        asked, _ = self._history_context(cache_dir)
        try:
            cached = parquet.load(symbol, cache_dir)
        except Exception as exc:
            log.warning("Invalid history cache for %s: %s; downloading full range", symbol, exc)
            cached = None
        plan = plan_history(cached, start, end, covered_start=asked.covers(symbol, start),
                            checked_end=asked.checked_through(symbol), force=self.force_refresh_history)
        return cached, plan

    def _fetch_and_cache_bars(
        self,
        symbol: str,
        timeframe: TimeFrame,
        start: date,
        end: date,
        cache_dir: Path,
    ) -> pd.DataFrame:
        asked, bucket = self._history_context(cache_dir)
        cached, plan = self._load_plan(symbol, start, end, cache_dir)
        if plan.mode == "current":
            self._history_stats[bucket]["current"] += 1
            return cached
        parts = [cached] if plan.mode == "incremental" else []
        for range_start, range_end in plan.spans:
            self._history_stats[bucket]["requests"] += 1
            request = StockBarsRequest(
                symbol_or_symbols=symbol, timeframe=timeframe,
                start=datetime.combine(range_start, datetime.min.time()),
                # Alpaca interprets naive end times as UTC; midnight would
                # silently drop the final day's daily bar.
                end=datetime.combine(range_end, datetime.max.time()),
                feed=market_data_feed(), adjustment=_ADJUST,
            )
            bars = self._client.get_stock_bars(request)
            df = bars.df
            if df is not None and not df.empty:
                if isinstance(df.index, pd.MultiIndex):
                    df = df.xs(symbol, level="symbol")
                df = df.copy()
                df.index = pd.to_datetime(df.index, utc=True)
                df.index.name = "timestamp"
                parts.append(df.reindex(columns=_BAR_COLS))
        merged = merge_history(*parts)
        if not merged.empty:
            parquet.save(symbol, merged, cache_dir)
        asked.note(symbol, start, end)
        asked.save()
        self._history_stats[bucket][plan.mode] += 1
        return merged

    def get_historical_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        df = self._fetch_and_cache_bars(symbol, TimeFrame.Day, start, end, self._cache_dir)
        return df

    def get_historical_bars(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Fetch intraday bars (1-min or 5-min) with parquet caching.

        Daily and intraday caches live in separate directories so the same
        symbol name can be used as the cache key in both without conflict.
        """
        alpaca_tf = _ALPACA_TIMEFRAME[timeframe]
        df = self._fetch_and_cache_bars(symbol, alpaca_tf, start, end, self._intraday_cache_dir)
        return df

    def subscribe_minute_bars(self, symbols: list[str], callback: Callable[[dict], None]) -> None:
        """Subscribe to 1-min bar closes via Alpaca WebSocket.

        Blocks until stop_stream() is called or the connection drops.
        The async WebSocket handler is bridged to the synchronous callback.

        Subscriptions are batched in groups of 100 to avoid Alpaca's WebSocket
        message size limit (~16 KB per subscribe request).
        """
        from alpaca.data.live import StockDataStream

        _BATCH = 100

        stream = StockDataStream(
            os.environ["ALPACA_API_KEY"],
            os.environ["ALPACA_SECRET_KEY"],
            feed=market_data_feed(),
        )
        self._stream = stream

        async def _handler(bar) -> None:
            try:
                if any(v is None for v in (bar.open, bar.high, bar.low, bar.close, bar.volume)):
                    log.debug("Skipping bar with None field(s) for %s", bar.symbol)
                    return
                callback({
                    "symbol":    bar.symbol,
                    "timestamp": bar.timestamp,
                    "open":      float(bar.open),
                    "high":      float(bar.high),
                    "low":       float(bar.low),
                    "close":     float(bar.close),
                    "volume":    float(bar.volume),
                })
            except Exception as exc:
                log.error("Stream callback error for %s: %s", bar.symbol, exc, exc_info=True)

        for i in range(0, len(symbols), _BATCH):
            batch = symbols[i : i + _BATCH]
            stream.subscribe_bars(_handler, *batch)
        log.info("Subscribed to %d symbols in %d batch(es) — starting stream",
                 len(symbols), (len(symbols) + _BATCH - 1) // _BATCH)
        stream.run()  # blocks until stop_stream() is called

    def stop_stream(self) -> None:
        """Stop the WebSocket stream gracefully."""
        stream = getattr(self, "_stream", None)
        if stream is not None:
            try:
                stream.stop()
            except TimeoutError:
                pass  # loop already stopped; Alpaca's finally: self.stop() is a no-op
            log.info("Stream stopped")

    def get_bars_range(
        self,
        symbol: str,
        timeframe: Timeframe,
        start: date,
        end: date,
    ) -> pd.DataFrame:
        """Fetch bars for a date range without caching. Used by the chart API."""
        alpaca_tf = _ALPACA_TIMEFRAME[timeframe]
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=alpaca_tf,
            start=datetime.combine(start, datetime.min.time()),
            end=datetime.combine(end, datetime.max.time()),
            feed=market_data_feed(),
            adjustment=_ADJUST,
        )
        bars = self._client.get_stock_bars(request)
        df = bars.df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df.index = pd.to_datetime(df.index, utc=True)
        df.index.name = "timestamp"
        return df.reindex(columns=_BAR_COLS)

    def get_todays_bars(self, symbol: str, timeframe: Timeframe) -> pd.DataFrame:
        """Fetch today's completed intraday bars without caching.

        Used to seed session state (e.g. SPY VWAP) at scanner startup so that
        indicators are computed from the full session, not just from first live bar.
        """
        today_et = datetime.now(_ET).date()
        alpaca_tf = _ALPACA_TIMEFRAME[timeframe]
        # Use ET-aware 4 AM start so Alpaca doesn't return previous evening's
        # after-hours bars (naive midnight would be interpreted as UTC = 8 PM ET yesterday)
        start_et = datetime(today_et.year, today_et.month, today_et.day, 4, 0, tzinfo=_ET)
        end_et   = datetime(today_et.year, today_et.month, today_et.day, 23, 59, 59, tzinfo=_ET)
        request = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=alpaca_tf,
            start=start_et,
            end=end_et,
            feed=market_data_feed(),
            adjustment=_ADJUST,
        )
        bars = self._client.get_stock_bars(request)
        df = bars.df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df.index = pd.to_datetime(df.index, utc=True)
        df.index.name = "timestamp"
        return df.reindex(columns=_BAR_COLS)

    # ── batched history (warmup) ─────────────────────────────────────────────
    #
    # The per-symbol get_historical_* methods above are one REST call each. Fine
    # at a few hundred symbols, hopeless at a few thousand: warmup becomes
    # thousands of serial round trips. These fetch the same data in multi-symbol
    # batches, write byte-identical parquet, and skip whatever the cache already
    # has, so a warm morning still costs nothing.

    def _request_bars_batch(self, symbols: list[str], timeframe: TimeFrame,
                            start: date, end: date, attempts: int = 5) -> pd.DataFrame:
        """One multi-symbol request, retrying on rate limits and transient errors.

        There is no 429 handling anywhere else in this file, which was
        survivable while warmup was serial and slow. Batched and threaded it is
        not.
        """
        req = StockBarsRequest(
            symbol_or_symbols=symbols,
            timeframe=timeframe,
            start=datetime.combine(start, datetime.min.time()),
            end=datetime.combine(end, datetime.max.time()),
            feed=market_data_feed(),
            adjustment=_ADJUST,
        )
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                return self._client.get_stock_bars(req).df
            except Exception as exc:
                msg = str(exc)
                retryable = "429" in msg or "rate" in msg.lower() or "timeout" in msg.lower()
                if not retryable or attempt == attempts:
                    raise
                # Jitter so parallel workers do not retry in lockstep.
                sleep = delay + random.uniform(0, delay / 2)
                log.warning("bars batch attempt %d/%d failed (%s); retrying in %.1fs",
                            attempt, attempts, msg[:120], sleep)
                time.sleep(sleep)
                delay = min(delay * 2, 30.0)
        return pd.DataFrame()

    def _fetch_bars_multi(self, symbols: list[str], timeframe: TimeFrame,
                          start: date, end: date, cache_dir: Path,
                          batch: int, workers: int,
                          progress: Optional[Callable[[int, int], None]] = None,
                          ) -> dict[str, pd.DataFrame]:
        """Batch symbols by the *missing interval*, preserving valid cached bars."""
        out: dict[str, pd.DataFrame] = {}
        pending: dict[str, tuple[pd.DataFrame | None, HistoryPlan]] = {}
        intervals: dict[tuple[date, date], list[str]] = defaultdict(list)
        asked, bucket = self._history_context(cache_dir)
        for sym in symbols:
            cached, plan = self._load_plan(sym, start, end, cache_dir)
            if plan.mode == "current":
                out[sym] = cached
                self._history_stats[bucket]["current"] += 1
                continue
            pending[sym] = (cached, plan)
            for span in plan.spans:
                intervals[span].append(sym)

        log.info("%s: %d current, %d to update in %d ranges", cache_dir.name,
                 len(out), len(pending), len(intervals))
        if progress:
            progress(len(out), len(symbols))
        if not pending:
            return out

        batches = [(span, group[i:i + batch]) for span, group in intervals.items()
                   for i in range(0, len(group), batch)]
        self._history_stats[bucket]["requests"] += len(batches)
        done = len(out)
        remaining = {sym: len(plan.spans) for sym, (_, plan) in pending.items()}
        pieces: dict[str, list[pd.DataFrame]] = defaultdict(list)
        failed: set[str] = set()

        def _one(span: tuple[date, date], group: list[str]) -> dict[str, pd.DataFrame]:
            df = self._request_bars_batch(group, timeframe, *span)
            got: dict[str, pd.DataFrame] = {}
            if df is None or df.empty:
                return got
            if isinstance(df.index, pd.MultiIndex):
                for sym in df.index.get_level_values(0).unique():
                    d = df.xs(sym, level=0).copy()
                    d.index = pd.to_datetime(d.index, utc=True)
                    d.index.name = "timestamp"
                    got[str(sym)] = d.reindex(columns=_BAR_COLS)
            elif len(group) == 1:
                d = df.copy()
                d.index = pd.to_datetime(d.index, utc=True)
                d.index.name = "timestamp"
                got[group[0]] = d.reindex(columns=_BAR_COLS)
            return got

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {pool.submit(_one, span, group): group for span, group in batches}
            for fut in as_completed(futs):
                group = futs[fut]
                try:
                    got = fut.result()
                    for sym, bars in got.items():
                        pieces[sym].append(bars)
                except Exception as exc:
                    log.warning("bars batch of %d failed permanently: %s", len(group), exc)
                    failed.update(group)
                for sym in group:
                    remaining[sym] -= 1
                    if remaining[sym] == 0:
                        done += 1
                if progress:
                    progress(min(done, len(symbols)), len(symbols))
        for sym, (cached, plan) in pending.items():
            if sym in failed:
                continue
            frames = ([cached] if plan.mode == "incremental" else []) + pieces[sym]
            merged = merge_history(*frames)
            if not merged.empty:
                parquet.save(sym, merged, cache_dir)
                out[sym] = merged
            asked.note(sym, start, end)
            self._history_stats[bucket][plan.mode] += 1
        asked.save()
        return out

    def get_historical_daily_multi(self, symbols: list[str], start: date, end: date,
                                   workers: int = 8,
                                   progress: Optional[Callable[[int, int], None]] = None,
                                   ) -> dict[str, pd.DataFrame]:
        """Daily bars for many symbols. Batches are large: 60 days x 500 symbols
        is only ~30k bars, comfortably inside the page limit."""
        return self._fetch_bars_multi(symbols, TimeFrame.Day, start, end,
                                      self._cache_dir, batch=500, workers=workers,
                                      progress=progress)

    def get_historical_bars_multi(self, symbols: list[str], timeframe: Timeframe,
                                  start: date, end: date, workers: int = 8,
                                  progress: Optional[Callable[[int, int], None]] = None,
                                  ) -> dict[str, pd.DataFrame]:
        """Intraday bars for many symbols.

        Batches are far smaller than the daily ones because of the 10,000-bar
        page limit: 20 days of 5-min bars is ~1,560 bars per symbol, so 50
        symbols is already ~78k bars. The SDK pages for us; a small batch keeps
        each response manageable and failures cheap to retry.
        """
        return self._fetch_bars_multi(symbols, _ALPACA_TIMEFRAME[timeframe], start, end,
                                      self._intraday_cache_dir, batch=50, workers=workers,
                                      progress=progress)

    def get_todays_bars_multi(
        self,
        symbols: list[str],
        timeframe: Timeframe = "1Min",
    ) -> dict[str, pd.DataFrame]:
        """Fetch today's intraday bars for many symbols in batched API calls.

        Returns {symbol: DataFrame} with UTC-indexed OHLCV columns.  Symbols
        with no bars today (e.g. not yet traded) are omitted from the result.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        today_et  = datetime.now(_ET).date()
        start_et  = datetime(today_et.year, today_et.month, today_et.day, 4, 0, tzinfo=_ET)
        end_et    = datetime(today_et.year, today_et.month, today_et.day, 23, 59, 59, tzinfo=_ET)
        alpaca_tf = _ALPACA_TIMEFRAME[timeframe]
        _BATCH = 100  # symbols per request — keeps individual payloads manageable

        def _fetch(batch: list[str]) -> dict[str, pd.DataFrame]:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=alpaca_tf,
                start=start_et,
                end=end_et,
                feed=market_data_feed(),
            adjustment=_ADJUST,
            )
            bars = self._client.get_stock_bars(req)
            df = bars.df
            if df.empty:
                return {}
            out: dict[str, pd.DataFrame] = {}
            if isinstance(df.index, pd.MultiIndex):
                for sym in df.index.get_level_values(0).unique():
                    sym_df = df.xs(sym, level=0).copy()
                    sym_df.index = pd.to_datetime(sym_df.index, utc=True)
                    out[str(sym)] = sym_df[["open", "high", "low", "close", "volume"]]
            elif len(batch) == 1:
                df.index = pd.to_datetime(df.index, utc=True)
                out[batch[0]] = df[["open", "high", "low", "close", "volume"]]
            return out

        batches = [symbols[i : i + _BATCH] for i in range(0, len(symbols), _BATCH)]
        result: dict[str, pd.DataFrame] = {}
        with ThreadPoolExecutor(max_workers=6) as pool:
            futs = {pool.submit(_fetch, b): b for b in batches}
            for fut in as_completed(futs):
                try:
                    result.update(fut.result())
                except Exception as exc:
                    log.warning("Today's bars batch failed: %s", exc)
        return result

    def get_snapshot(self, symbols: list[str]) -> dict[str, dict]:
        """Return latest trade price, bid, and ask for each symbol."""
        from alpaca.data.requests import StockSnapshotRequest

        req = StockSnapshotRequest(
            symbol_or_symbols=symbols,
            feed=market_data_feed(),
        )
        snaps = self._client.get_stock_snapshot(req)

        result: dict[str, dict] = {}
        for sym, snap in snaps.items():
            try:
                price     = float(snap.latest_trade.price) if snap.latest_trade else None
                bid       = float(snap.latest_quote.bid_price) if snap.latest_quote else None
                ask       = float(snap.latest_quote.ask_price) if snap.latest_quote else None
                daily_vol = float(snap.daily_bar.volume) if snap.daily_bar else None
            except (AttributeError, TypeError, ValueError):
                log.warning("Snapshot parse error for %s — skipping", sym)
                continue
            result[sym] = {"price": price, "bid": bid, "ask": ask, "daily_volume": daily_vol}
        return result
