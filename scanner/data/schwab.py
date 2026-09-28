"""DataFeed backed by the Charles Schwab API (via the schwabdev wrapper).

Selected with DATA_PROVIDER=schwab in .env (or run_live.py --feed schwab);
AlpacaFeed remains the default. Needs `pip install schwabdev` and a one-time
login with scripts/schwab_auth.py (repeat weekly: Schwab expires it after 7 days).

Why this exists: Alpaca allows ONE concurrent market-data websocket per
account, and costs $99/mo. Schwab is free with a brokerage account and gives
an independent stream. Whether the DATA is equivalent is a separate question,
answered by scripts/check_schwab_match.py, not by this file.

Known differences from AlpacaFeed, all deliberate:
  * Cache lives under data/schwab/** so it can never mix with Alpaca's parquet
    cache. Mixing them would silently corrupt the live scanner's history.
  * Schwab's price-history endpoint is ONE SYMBOL PER REQUEST (no batch
    variant), so multi-symbol fetches are threaded and throttled instead of
    batched. Expect slower cold warmups.
  * Schwab returns no vwap / trade_count on candles; those columns are present
    but NaN so the DataFrame schema still matches AlpacaFeed.
  * Minute history has a shorter lookback than Alpaca's. Verify empirically
    with check_schwab_match.py before relying on it for the 20-day RVOL profile.
  * The refresh token expires every 7 days and re-auth opens a browser
    (Schwab's rule, not schwabdev's). Unattended runs WILL eventually stop.
  * SCHWAB STREAMS 1-MINUTE BARS FOR AT MOST 300 SYMBOLS PER ACCOUNT
    (CHART_EQUITY; measured 2026-09-20, the streamer answers code 19 and
    discards the rest). Alpaca's paid feed has no such cap. Symbols past the cap
    get no live bars, so subscribe_minute_bars takes them in the order given
    (the universe file is sorted by dollar volume, most liquid first), says
    loudly how many were left out, and exposes them as `unstreamed_symbols`.

Credentials (add to .env yourself, never commit):
    SCHWAB_APP_KEY=...
    SCHWAB_APP_SECRET=...
    SCHWAB_CALLBACK_URL=https://127.0.0.1
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd
from dotenv import load_dotenv

from scanner.cache import parquet
from scanner.cache.history import RequestedCoverage, merge_history, plan_history
from scanner.data.interface import DataFeed, Timeframe

load_dotenv(override=True)

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

# Isolated from Alpaca's data/daily + data/5m. Do NOT point these at the Alpaca
# dirs: the two providers disagree on volume and would poison the live cache.
_DEFAULT_DAILY_CACHE    = Path("data/schwab/daily")
_DEFAULT_INTRADAY_CACHE = Path("data/schwab/5m")

_BAR_COLS = ["open", "high", "low", "close", "volume", "vwap", "trade_count"]

# (periodType, frequencyType, frequency). periodType is NOT optional: Schwab
# defaults it to "day", and "day" only permits frequencyType="minute", so
# asking for daily candles without it returns 400 Bad Request. Valid pairs:
#     day -> minute        month -> daily, weekly
#     year -> daily, weekly, monthly     ytd -> daily, weekly
# Minute frequencies are limited to 1, 5, 10, 15, 30 (no 60), so 1Hour is
# resampled from 30-minute candles.
_FREQ: dict[str, tuple[str, str, int]] = {
    "1Min":  ("day",  "minute", 1),
    "5Min":  ("day",  "minute", 5),
    "15Min": ("day",  "minute", 15),
    "30Min": ("day",  "minute", 30),
    "Day":   ("year", "daily",  1),
    "1Week": ("year", "weekly", 1),
}

_MAX_WORKERS = 4      # concurrency; the RATE is set by _LIMITER, not by this

# Schwab's market-data API allows about 120 requests a minute per app. Every
# request in this module goes through one shared limiter so that threads
# together stay under it. Override with SCHWAB_MAX_RPM if Schwab raises yours.
_MAX_RPM = float(os.environ.get("SCHWAB_MAX_RPM", "110"))
_RETRIES = 4                 # on HTTP 429 / 5xx, with exponential backoff
_REFRESH_TOKEN_DAYS = 7      # Schwab's rule; after this a browser login is required
_TOKENS_DB = Path(os.path.expanduser("~/.schwabdev/tokens.db"))


class _RateLimiter:
    """Evenly spaced, thread-safe: at most `rpm` acquisitions per minute."""

    def __init__(self, rpm: float, clock=time.monotonic, sleep=time.sleep) -> None:
        self._interval = 60.0 / max(rpm, 1.0)
        self._next = 0.0
        self._lock = threading.Lock()
        self._clock, self._sleep = clock, sleep

    def acquire(self) -> None:
        with self._lock:
            now = self._clock()
            wait = self._next - now
            self._next = max(now, self._next) + self._interval
        if wait > 0:
            self._sleep(wait)


_LIMITER = _RateLimiter(_MAX_RPM)


def refresh_token_age_days(db: Path = _TOKENS_DB) -> float | None:
    """Days since Schwab last issued the refresh token, or None if unknown.
    Reads only the timestamp column, never a token."""
    try:
        import sqlite3
        with sqlite3.connect(db) as con:
            row = con.execute("select refresh_token_issued from schwabdev").fetchone()
        issued = datetime.fromisoformat(row[0])
        return (datetime.now(issued.tzinfo) - issued).total_seconds() / 86400
    except Exception:
        return None


def _num(v) -> float | None:
    """A quote field as a float, or None when it is missing or not a number."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None


_Asked = RequestedCoverage


class SchwabFeed(DataFeed):
    """DataFeed implementation over the Schwab market-data API."""

    supports_trade_updates = True

    def __init__(
        self,
        cache_dir: Path = _DEFAULT_DAILY_CACHE,
        intraday_cache_dir: Path = _DEFAULT_INTRADAY_CACHE,
        client=None,
        force_refresh_history: bool = False,
    ) -> None:
        self._cache_dir = Path(cache_dir)
        self._intraday_cache_dir = Path(intraday_cache_dir)
        self._asked_daily = _Asked(self._cache_dir)
        self._asked_intraday = _Asked(self._intraday_cache_dir)
        self._in_batch = False
        self.force_refresh_history = force_refresh_history
        self._history_stats = {"daily": {"current": 0, "incremental": 0, "full": 0, "requests": 0},
                               "5m": {"current": 0, "incremental": 0, "full": 0, "requests": 0}}
        self._stats_lock = threading.Lock()
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._intraday_cache_dir.mkdir(parents=True, exist_ok=True)

        if client is not None:
            self._client = client            # injected (tests)
        else:
            age = refresh_token_age_days()
            registered_callback = os.environ.get("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
            callback_has_trailing_slash = registered_callback.endswith("/")
            if callback_has_trailing_slash and age is None:
                raise RuntimeError(
                    "Schwabdev rejects the registered callback URL ending in '/'; no imported "
                    "schwabdev token was found. Refresh the shared SCHWAB_TOKEN_PATH login "
                    "with its original client and import it before starting Edge."
                )
            if callback_has_trailing_slash and age >= _REFRESH_TOKEN_DAYS - 61 / (24 * 60):
                raise RuntimeError(
                    "The shared Schwab login is due for renewal. Schwabdev cannot perform a "
                    "new login with the registered callback URL ending in '/'. Renew the "
                    "SCHWAB_TOKEN_PATH login with its original client before starting Edge."
                )
            if age is not None and age >= _REFRESH_TOKEN_DAYS:
                # schwabdev would otherwise stop at a console prompt waiting
                # for a browser login, which hangs an unattended start.
                raise RuntimeError(
                    f"Schwab login expired ({age:.0f} days since the last login; Schwab allows "
                    f"{_REFRESH_TOKEN_DAYS}). Run: python scripts/schwab_auth.py")
            if age is not None and age >= _REFRESH_TOKEN_DAYS - 1:
                log.warning("Schwab login expires within a day; run scripts/schwab_auth.py soon")
            import schwabdev
            key    = os.environ.get("SCHWAB_APP_KEY")
            secret = os.environ.get("SCHWAB_APP_SECRET")
            if not key or not secret:
                raise RuntimeError(
                    "SCHWAB_APP_KEY / SCHWAB_APP_SECRET missing from .env. "
                    "Register an app at developer.schwab.com, then add them."
                )
            self._client = schwabdev.Client(
                app_key=key,
                app_secret=secret,
                # Schwabdev rejects a trailing slash even when using an existing
                # token. Its refresh-token grant omits redirect_uri; this shim
                # is not suitable for a new authorization-code login.
                callback_url=registered_callback.rstrip("/") if callback_has_trailing_slash
                             else registered_callback,
            )
        self._stream = None
        self._stop_evt = threading.Event()
        self.streamed_symbols: list[str] = []      # live 1-min bars (Schwab caps these)
        self.unstreamed_symbols: list[str] = []    # asked for, but past Schwab's cap
        self.quote_streamed_symbols: list[str] = []   # bars built from the live quote stream
        self.polled_symbols: list[str] = []           # bars built from polled quotes
        self.quote_bars = None                        # the QuoteBarBuilder, once streaming
        from scanner.quote_state import QuoteBook
        from scanner.schwab_screener import SchwabScreenerBook
        self.quote_book = QuoteBook()
        from scanner.discovery_sessions import DiscoverySessionStore
        try:
            discovery_retention = int(os.environ.get("SCHWAB_DISCOVERY_RETENTION", "20"))
        except ValueError:
            discovery_retention = 20
        self.screener_book = SchwabScreenerBook(session_store=DiscoverySessionStore(
            Path(os.environ.get("SCHWAB_DISCOVERY_PATH", "data/schwab_discovery/sessions.jsonl")),
            retention_sessions=discovery_retention,
        ))
        self.trade_callback = None  # set by LiveScanner before the single stream starts
        self.quote_covered_symbols: list[str] = []
        self._quote_lock = threading.RLock()
        self._screener_lock = threading.RLock()
        self._screener_keys: list[str] = []
        self._quote_base_symbols: set[str] = set()
        self._quote_watched_symbols: set[str] = set()
        self._dynamic_lock = threading.RLock()
        self._dynamic_memberships: dict[str, dict] = {}
        self._dynamic_requests: dict[str, tuple[str, str, str]] = {}
        self._dynamic_controller = None
        self._dynamic_connection_epoch = 0
        self._dynamic_connected = False
        self._dynamic_ever_connected = False
        self._dynamic_health = {
            "CHART_EQUITY": {"status": "unavailable", "error": None},
            "LEVELONE_EQUITIES": {"status": "unavailable", "error": None},
        }
        self._base_requests: dict[str, tuple[str, list[str]]] = {}
        self._base_acknowledged = {"CHART_EQUITY": set(), "LEVELONE_EQUITIES": set()}
        self._base_rejected = {"CHART_EQUITY": set(), "LEVELONE_EQUITIES": set()}
        self._base_tracking = {"CHART_EQUITY": False, "LEVELONE_EQUITIES": False}
        self._dynamic_caps = {"CHART_EQUITY": self.CHART_EQUITY_CAP,
                              "LEVELONE_EQUITIES": self.LEVELONE_CAP}
        try:
            self._dynamic_chart_headroom = max(0, int(os.environ.get("SCHWAB_CHART_HEADROOM", "5")))
        except ValueError:
            self._dynamic_chart_headroom = 5

    # ── Candle parsing ────────────────────────────────────────────────────────

    @staticmethod
    def _candles_to_df(payload: dict) -> pd.DataFrame:
        """Schwab candle JSON -> UTC-indexed OHLCV frame matching AlpacaFeed."""
        candles = (payload or {}).get("candles") or []
        if not candles:
            return pd.DataFrame(columns=_BAR_COLS).rename_axis("timestamp")
        df = pd.DataFrame(candles)
        df["timestamp"] = pd.to_datetime(df["datetime"], unit="ms", utc=True)
        df = df.set_index("timestamp").sort_index()
        # Schwab gives no vwap / trade_count; keep the columns so downstream code
        # that reindexes on _BAR_COLS behaves identically to Alpaca.
        for col in ("vwap", "trade_count"):
            if col not in df.columns:
                df[col] = float("nan")
        return df.reindex(columns=_BAR_COLS)

    def _price_history(self, symbol: str, timeframe: Timeframe,
                       start: date, end: date) -> pd.DataFrame:
        if timeframe in ("1Hour", "4Hour"):
            # Schwab has no hourly candles: build them from 30-minute ones.
            half = self._price_history(symbol, "30Min", start, end)
            if half.empty:
                return half
            return half.resample("1h" if timeframe == "1Hour" else "4h").agg({
                "open": "first", "high": "max", "low": "min", "close": "last",
                "volume": "sum", "vwap": "mean", "trade_count": "sum",
            }).dropna(subset=["open"])

        ptype, ftype, freq = _FREQ[timeframe]
        # period is deliberately omitted: Schwab rejects it alongside
        # startDate/endDate, which is how this feed always queries.
        resp = self._request(lambda: self._client.price_history(
            symbol=symbol,
            periodType=ptype,
            frequencyType=ftype,
            frequency=freq,
            startDate=datetime.combine(start, datetime.min.time()),
            endDate=datetime.combine(end, datetime.max.time()),
            needExtendedHoursData=(ftype == "minute"),   # premarket levels need it
        ))
        return self._candles_to_df(resp.json())

    def _request(self, call):
        """Rate-limited call; recover one stale REST token, then retry 429/5xx."""
        delay = 2.0
        auth_retried = False
        for attempt in range(_RETRIES + 1):
            _LIMITER.acquire()
            resp = call()
            status = getattr(resp, "status_code", 200)
            if status == 401 and not auth_retried:
                # Another client may have refreshed the shared schwabdev DB
                # while this client's session still holds an old auth header.
                # The public method adopts that newer token (or refreshes an
                # invalid one) without opening another market-data stream.
                auth_retried = True
                update = getattr(self._client, "update_tokens", None)
                if callable(update):
                    try:
                        updated = update(force_access_token=True)
                    except Exception as exc:
                        log.warning("Schwab REST auth recovery failed (%s)", type(exc).__name__)
                        updated = False
                    if updated:
                        _LIMITER.acquire()
                        resp = call()
                        status = getattr(resp, "status_code", 200)
            if status == 429 or status >= 500:
                if attempt == _RETRIES:
                    resp.raise_for_status()
                log.debug("Schwab HTTP %s, retrying in %.0fs", status, delay)
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
                continue
            resp.raise_for_status()
            return resp
        return resp

    # ── DataFeed interface ────────────────────────────────────────────────────

    def get_historical_daily(self, symbol: str, start: date, end: date) -> pd.DataFrame:
        return self._cached_history(symbol, "Day", start, end, self._cache_dir, self._asked_daily, "daily")

    def history_stats(self) -> dict:
        with self._stats_lock:
            return json.loads(json.dumps(self._history_stats))

    def get_historical_bars(self, symbol: str, timeframe: Timeframe,
                            start: date, end: date) -> pd.DataFrame:
        return self._cached_history(symbol, timeframe, start, end, self._intraday_cache_dir,
                                    self._asked_intraday, "5m")

    def _cached_history(self, symbol: str, timeframe: Timeframe, start: date, end: date,
                        cache_dir: Path, asked: _Asked, bucket: str) -> pd.DataFrame:
        try:
            cached = parquet.load(symbol, cache_dir)
        except Exception as exc:
            log.warning("Invalid history cache for %s: %s; downloading full range", symbol, exc)
            cached = None
        plan = plan_history(cached, start, end, covered_start=asked.covers(symbol, start),
                            checked_end=asked.checked_through(symbol), force=self.force_refresh_history)
        if plan.mode == "current":
            with self._stats_lock:
                self._history_stats[bucket]["current"] += 1
            return cached
        parts = [cached] if plan.mode == "incremental" else []
        for range_start, range_end in plan.spans:
            with self._stats_lock:
                self._history_stats[bucket]["requests"] += 1
            parts.append(self._price_history(symbol, timeframe, range_start, range_end))
        df = merge_history(*parts)
        if not df.empty:
            parquet.save(symbol, df, cache_dir)
        asked.note(symbol, start, end)
        if not self._in_batch:
            asked.save()
        with self._stats_lock:
            self._history_stats[bucket][plan.mode] += 1
        return df

    def get_bars_range(self, symbol: str, timeframe: Timeframe,
                       start: date, end: date) -> pd.DataFrame:
        """Uncached range fetch (used by the chart API)."""
        return self._price_history(symbol, timeframe, start, end)

    def get_todays_bars(self, symbol: str, timeframe: Timeframe) -> pd.DataFrame:
        today = datetime.now(_ET).date()
        return self._price_history(symbol, timeframe, today, today)

    def get_todays_bars_multi(self, symbols: list[str],
                              timeframe: Timeframe = "1Min") -> dict[str, pd.DataFrame]:
        """One request per symbol (Schwab has no batch endpoint), threaded.

        At about 120 requests a minute a whole-market universe would hold the
        start up for close to an hour, so only the first SEED_MAX_SYMBOLS are
        seeded (the caller passes them most liquid first). The rest build their
        session from the live feed: complete when the scanner starts before the
        open, and from the start time onward when it starts mid-session.
        """
        if len(symbols) > self.SEED_MAX_SYMBOLS:
            print(f"       Schwab: seeding today's bars for the {self.SEED_MAX_SYMBOLS} most liquid symbols "
                  f"only (one request each). The other {len(symbols) - self.SEED_MAX_SYMBOLS:,} build VWAP, "
                  f"volume and levels from the live feed, so start before the open.", flush=True)
            symbols = symbols[: self.SEED_MAX_SYMBOLS]
        today = datetime.now(_ET).date()
        out: dict[str, pd.DataFrame] = {}

        def _one(sym: str):
            return sym, self._price_history(sym, timeframe, today, today)

        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futs = [pool.submit(_one, s) for s in symbols]
            for fut in as_completed(futs):
                try:
                    sym, df = fut.result()
                    if not df.empty:
                        out[sym] = df[["open", "high", "low", "close", "volume"]]
                except Exception as exc:
                    log.warning("Schwab today's bars failed: %s", exc)
        return out

    def _multi(self, fetch, symbols: list[str], progress=None) -> dict[str, pd.DataFrame]:
        """Run fetch(symbol) for many symbols on a few threads. The shared rate
        limiter, not the thread count, sets the pace; failures are logged and
        skipped so one bad symbol never stops a warmup."""
        out: dict[str, pd.DataFrame] = {}
        done = 0
        self._in_batch = True             # one write of the request record, at the end
        with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as pool:
            futs = {pool.submit(fetch, s): s for s in symbols}
            for fut in as_completed(futs):
                sym = futs[fut]
                try:
                    df = fut.result()
                    if df is not None and not df.empty:
                        out[sym] = df
                except Exception as exc:
                    log.warning("Schwab history failed for %s: %s", sym, exc)
                done += 1
                if progress is not None:
                    progress(done, len(symbols))
        self._in_batch = False
        self._asked_daily.save()
        self._asked_intraday.save()
        return out

    def get_historical_daily_multi(self, symbols: list[str], start: date, end: date,
                                   workers: int = 0, progress=None) -> dict[str, pd.DataFrame]:
        """Daily bars for many symbols, one request each (Schwab has no batch
        endpoint): about len(symbols) / SCHWAB_MAX_RPM minutes when not cached."""
        return self._multi(lambda s: self.get_historical_daily(s, start, end), symbols, progress)

    def get_historical_bars_multi(self, symbols: list[str], timeframe: Timeframe,
                                  start: date, end: date, workers: int = 0,
                                  progress=None) -> dict[str, pd.DataFrame]:
        return self._multi(lambda s: self.get_historical_bars(s, timeframe, start, end), symbols, progress)

    def get_session_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """The session so far, from quotes: open, high, low, last and total volume.

        For a mid-session start. Bars for today can only be back-filled for the
        most liquid symbols (one request each), which would leave every other
        symbol starting the day at zero volume, so its relative volume reads far
        too low until the close. Quotes come 500 to a request, so the whole
        universe takes seconds. What this cannot give is the session VWAP (it is
        approximated by the day's typical price) or the split between premarket
        and regular volume (total volume includes both).
        """
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), self.QUOTES_PER_REQUEST):
            batch = symbols[i : i + self.QUOTES_PER_REQUEST]
            try:
                resp = self._request(lambda b=batch: self._client.quotes(symbols=b, fields="quote"))
                for sym, payload in (resp.json() or {}).items():
                    q = (payload or {}).get("quote") or {}
                    row = {"open": _num(q.get("openPrice")), "high": _num(q.get("highPrice")),
                           "low": _num(q.get("lowPrice")), "last": _num(q.get("lastPrice")),
                           "volume": _num(q.get("totalVolume"))}
                    if all(v is not None and v > 0 for v in row.values()):
                        out[sym] = row
            except Exception as exc:
                log.warning("Schwab session quotes failed for %d symbols: %s", len(batch), exc)
        return out

    def get_snapshot(self, symbols: list[str]) -> dict[str, dict]:
        result: dict[str, dict] = {}
        _BATCH = 100   # quotes() IS batched, unlike price history
        for i in range(0, len(symbols), _BATCH):
            batch = symbols[i : i + _BATCH]
            try:
                resp = self._request(lambda b=batch: self._client.quotes(symbols=b, fields="quote"))
                for sym, payload in (resp.json() or {}).items():
                    q = (payload or {}).get("quote") or {}
                    result[sym] = {
                        "price":        q.get("lastPrice"),
                        "bid":          q.get("bidPrice"),
                        "ask":          q.get("askPrice"),
                        "daily_volume": q.get("totalVolume"),
                    }
            except Exception as exc:
                log.warning("Schwab quotes batch failed: %s", exc)
        return result

    def get_fundamentals(self, symbols: list[str]) -> dict[str, dict]:
        """Return Schwab instrument rows keyed by symbol.

        The Instruments endpoint accepts multiple comma-separated symbols.  Keep
        these requests batched: the dashboard prefetch can cover a whole
        universe without turning one symbol into one REST call.
        """
        result: dict[str, dict] = {}
        batch_size = 100
        clean_symbols = [str(symbol).upper().strip() for symbol in symbols if str(symbol).strip()]
        for i in range(0, len(clean_symbols), batch_size):
            batch = clean_symbols[i : i + batch_size]
            try:
                resp = self._request(
                    lambda b=batch: self._client.instruments(",".join(b), projection="fundamental")
                )
                for row in (resp.json() or {}).get("instruments", []) or []:
                    if not isinstance(row, dict):
                        continue
                    sym = str(row.get("symbol") or "").upper().strip()
                    if sym:
                        result[sym] = row
            except Exception as exc:
                log.warning("Schwab fundamentals batch failed for %d symbols: %s", len(batch), exc)
        return result

    # ── Streaming ─────────────────────────────────────────────────────────────

    @staticmethod
    def handle_message(raw, callback: Callable[[dict], None]) -> None:
        """Decode one raw streamer message and emit any CHART_EQUITY bars.

        Split out from subscribe_minute_bars so it can be tested without
        opening a socket. Field order is 0 key, 1 sequence, 2 open, 3 high,
        4 low, 5 close, 6 volume, 7 chart time (epoch ms), 8 chart day --
        this is schwabdev's CORRECTED order; Schwab's own docs are wrong.
        Malformed payloads are skipped rather than killing the stream.
        """
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (TypeError, ValueError):
            return
        for block in (msg or {}).get("data", []) or []:
            if block.get("service") != "CHART_EQUITY":
                continue
            for c in block.get("content", []) or []:
                try:
                    callback({
                        "symbol":    c.get("key"),
                        "timestamp": pd.to_datetime(int(c["7"]), unit="ms", utc=True),
                        "source": "schwab_chart_equity",
                        "open":      float(c["2"]),
                        "high":      float(c["3"]),
                        "low":       float(c["4"]),
                        "close":     float(c["5"]),
                        "volume":    float(c["6"]),
                    })
                except (KeyError, TypeError, ValueError) as exc:
                    log.debug("Skipping malformed CHART_EQUITY payload: %s", exc)

    # Schwab's per-account limits, measured against the live streamer 2026-09-20.
    # The streamer's own answer (code 19) overrides these if Schwab changes them.
    CHART_EQUITY_CAP = 300        # real 1-minute bars
    LEVELONE_CAP = 3000           # live quotes
    QUOTES_PER_REQUEST = 500      # REST quotes
    POLL_SECONDS = 10.0           # one pass over the polled symbols
    # No batch history endpoint: callers that would re-download bars for the whole
    # universe (the premarket lists) must use the scanner's live state instead.
    per_symbol_history = True
    SEED_MAX_SYMBOLS = 600        # today's-bars seeding at startup: about 5 minutes

    @staticmethod
    def parse_symbol_cap(raw, service: str = "CHART_EQUITY") -> int | None:
        """The cap Schwab reports when a subscription exceeds it, else None.

        The streamer answers an over-limit ADD with code 19 and a message like
        "You've reached the maximum number of symbols allowed.  (CHART_EQUITY=300,
        DISCARDED=250)". It does NOT raise and keeps streaming the symbols it
        had already accepted, which is why this has to be read explicitly.
        """
        import re
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (TypeError, ValueError):
            return None
        for r in (msg or {}).get("response", []) or []:
            content = r.get("content") or {}
            if r.get("service") == service and content.get("code") == 19:
                m = re.search(service + r"=(\d+)", str(content.get("msg") or ""))
                if m:
                    return int(m.group(1))
                return SchwabFeed.CHART_EQUITY_CAP if service == "CHART_EQUITY" else SchwabFeed.LEVELONE_CAP
        return None

    @staticmethod
    def handle_quotes(raw, builder, book=None, on_trade=None) -> None:
        """Merge sparse Level One fields into the quote book and optional bar builder.

        Bid/ask are 1/2, trade 3, quoted sizes 4/5, trade size 9, quote time
        34, trade time 35 and individual side times 37/38 (epoch ms).
        Quote-only changes reach the book even without a new trade or bar.
        """
        try:
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (TypeError, ValueError):
            return
        for block in (msg or {}).get("data", []) or []:
            if block.get("service") != "LEVELONE_EQUITIES":
                continue
            for c in block.get("content", []) or []:
                try:
                    sym = c.get("key")
                    snap = None
                    if book is not None and sym:
                        fields = {}
                        for side, price_key, time_key, size_key in (
                            ("bid", "1", "37", "4"), ("ask", "2", "38", "5"),
                            ("last", "3", "35", "9"),
                        ):
                            if any(k in c for k in (price_key, time_key, size_key)):
                                # Field 34 is the combined quote clock, not a
                                # defensible per-side clock when both sides are
                                # present. Keep a side unverified if 37/38 is
                                # absent instead of inventing its freshness.
                                market_time = c.get(time_key)
                                value = float("nan") if price_key in c and c[price_key] is None else c.get(price_key)
                                fields[side] = (value, market_time, c.get(size_key))
                        snap = book.ingest(sym, fields, delayed=c.get("delayed"),
                                           block_ms=block.get("timestamp"))
                    # Sparse Level One updates retain prior price/time in the
                    # book. Only a raw update carrying BOTH fields is a new
                    # observed trade candidate; quote receipt is not trade time.
                    if on_trade is not None and snap is not None and "3" in c and "35" in c:
                        price, trade_ms = _num(c.get("3")), _num(c.get("35"))
                        if (price is not None and trade_ms is not None
                                and trade_ms > 1_000_000_000_000
                                and snap.get("last_market_ms") == int(trade_ms)
                                and snap.get("last") == price):
                            on_trade({"symbol": sym, "price": price,
                                      "trade_market_ms": int(trade_ms),
                                      "receipt_ms": snap["receipt_ms"],
                                      "stream_id": snap["stream_id"],
                                      "connection_epoch": snap["connection_epoch"],
                                      "source": snap["source"], "tier": snap["tier"],
                                      "coverage": snap["coverage"],
                                      "quality": snap["quality"],
                                      "delayed": snap["delayed"]})
                    if builder is not None:
                        builder.on_quote(sym, last=_num(c.get("3")), total_volume=_num(c.get("8")),
                                         day_high=_num(c.get("10")), day_low=_num(c.get("11")),
                                         last_size=_num(c.get("9")))
                except Exception as exc:
                    log.debug("Skipping malformed LEVELONE payload: %s", exc)

    def subscribe_minute_bars(self, symbols: list[str],
                              callback: Callable[[dict], None]) -> None:
        """Stream 1-minute bars for every symbol. Blocks until stop_stream().

        Schwab serves real 1-minute bars (CHART_EQUITY) for at most 300 symbols
        per account, so a larger universe is covered in three tiers, in the
        order given (the universe file is sorted most liquid first):

            first 300     real bars from CHART_EQUITY
            next slots   bars built from LEVELONE_EQUITIES; its 3,000 slots also
                         cover the first 300 CHART_EQUITY symbols
            the rest      bars built from REST quotes polled every POLL_SECONDS

        See scanner/data/quote_bars.py for how a bar is built from quotes and how
        close it is to a real one. Set SCHWAB_SYNTHETIC_BARS=0 to turn the second
        and third tier off: only the first 300 symbols are then scanned.

        CHART_EQUITY field order (schwabdev translate.py, corrected against
        Schwab's own docs which are wrong): 0 key, 1 sequence, 2 open, 3 high,
        4 low, 5 close, 6 volume, 7 chart time (epoch ms), 8 chart day.
        """
        from scanner.data.quote_bars import QuoteBarBuilder
        # schwabdev 4.0.0 exposes no Client.stream property; construct it.
        import schwabdev
        stream = schwabdev.Stream(self._client)
        self._stream = stream
        self._stop_evt.clear()

        # Bars now arrive from three threads (stream, quote flush, poller) and the
        # scanner's bar handler is not re-entrant: one bar at a time.
        emit_lock = threading.Lock()

        def _emit(bar: dict) -> None:
            with emit_lock:
                controller = self._dynamic_controller
                if controller is not None:
                    epoch = controller.bar_epoch(str(bar.get("symbol") or ""))
                    if epoch is not None:
                        bar = {**bar, "_dynamic_epoch": epoch,
                               "_connection_epoch": self._dynamic_connection_epoch}
                consumed = bool(controller and controller.on_bar(bar))
                if not consumed:
                    callback(bar)

        synthetic = os.environ.get("SCHWAB_SYNTHETIC_BARS", "1").strip().lower() not in ("0", "false", "no", "off")
        cap = self.CHART_EQUITY_CAP
        self.streamed_symbols = list(symbols[:cap])
        rest = list(symbols[cap:])
        quote_room = max(0, self.LEVELONE_CAP - len(self.streamed_symbols))
        self.quote_streamed_symbols = rest[:quote_room] if synthetic else []
        self.polled_symbols = rest[quote_room:] if synthetic else []
        self.unstreamed_symbols = [] if synthetic else rest
        # Quote coverage is independent of bar tier. The chart-bar symbols need
        # Level One too, and held symbols can be added later on this stream.
        with self._quote_lock:
            self.quote_covered_symbols = list(dict.fromkeys(symbols))[:self.LEVELONE_CAP]
            self._quote_base_symbols = set(self.quote_covered_symbols)
            for sym in self.quote_covered_symbols:
                self.quote_book.cover(sym)
            for sym in symbols[self.LEVELONE_CAP:]:
                self.quote_book.cover(sym, status="cap_exceeded")
        self.quote_bars = builder = QuoteBarBuilder(_emit)
        tiers_lock = threading.Lock()
        for sym in self.quote_streamed_symbols:
            builder.track(sym, grace=3.0)
        for sym in self.polled_symbols:
            builder.track(sym, grace=self.POLL_SECONDS + 5.0)

        def _receiver(raw) -> None:
            self.quote_book.stream_activity()
            self.screener_book.ingest(raw)
            self._ingest_dynamic_responses(raw)
            partial = self._ingest_base_responses(raw)
            chart_cap = self.parse_symbol_cap(raw, "CHART_EQUITY")
            if chart_cap is not None:
                with self._dynamic_lock:
                    self._dynamic_caps["CHART_EQUITY"] = chart_cap
            if chart_cap is not None and chart_cap < len(self.streamed_symbols):
                # Schwab accepted fewer real-bar symbols than expected: the overflow
                # moves down a tier (or is reported, with synthetic bars off).
                with tiers_lock:
                    protected = set(getattr(self._dynamic_controller, "protected_symbols", lambda: set())())
                    exact = partial.get("CHART_EQUITY", [])
                    over = [sym for sym in (exact or self.streamed_symbols[chart_cap:]) if sym not in protected]
                    kept_protected = [sym for sym in self.streamed_symbols[chart_cap:] if sym in protected]
                    self.streamed_symbols = self.streamed_symbols[:chart_cap] + kept_protected
                    if synthetic:
                        for sym in over:
                            builder.track(sym, grace=self.POLL_SECONDS + 5.0)
                        self.polled_symbols = over + self.polled_symbols
                    else:
                        self.unstreamed_symbols = over + self.unstreamed_symbols
                        self._warn_cap(chart_cap)
            quote_cap = self.parse_symbol_cap(raw, "LEVELONE_EQUITIES")
            if quote_cap is not None:
                with self._dynamic_lock:
                    self._dynamic_caps["LEVELONE_EQUITIES"] = quote_cap
            if quote_cap is not None and quote_cap < len(self.quote_covered_symbols):
                with tiers_lock:
                    with self._quote_lock:
                        protected = set(getattr(self._dynamic_controller, "protected_symbols", lambda: set())())
                        exact = partial.get("LEVELONE_EQUITIES", [])
                        over_covered = [sym for sym in (exact or self.quote_covered_symbols[quote_cap:])
                                        if sym not in protected]
                        kept_protected = [sym for sym in self.quote_covered_symbols[quote_cap:]
                                          if sym in protected]
                        self.quote_covered_symbols = self.quote_covered_symbols[:quote_cap] + kept_protected
                    for sym in over_covered:
                        self.quote_book.coverage(sym, "cap_exceeded")
                    over = [sym for sym in self.quote_streamed_symbols if sym not in self.quote_covered_symbols]
                    self.quote_streamed_symbols = [sym for sym in self.quote_streamed_symbols if sym in self.quote_covered_symbols]
                    for sym in over:
                        builder.track(sym, grace=self.POLL_SECONDS + 5.0)
                    self.polled_symbols = over + self.polled_symbols
                log.warning("Schwab accepted %d quote-stream symbols; %d moved to polling", quote_cap, len(over))
            self.handle_message(raw, _emit)
            def _trade(event: dict) -> None:
                callback = getattr(self, "trade_callback", None)
                if callback is not None:
                    event["stream_active"] = bool(getattr(stream, "active", False))
                    with emit_lock:
                        callback(event)
            self.handle_quotes(raw, builder if synthetic else None, self.quote_book,
                               _trade if getattr(self, "trade_callback", None) is not None else None)

        if self.unstreamed_symbols:
            self._warn_cap(cap)

        stream.start(receiver=_receiver, daemon=True)
        from scanner.schwab_screener import DEFAULT_SCREENER_KEYS
        with self._screener_lock:
            self._screener_keys = list(DEFAULT_SCREENER_KEYS)
        self.screener_book.requested(self._screener_keys)
        self._send_screener_requests(stream, self._screener_keys, "0,1,2,3,4", "ADD")
        # Subscribe in chunks; Schwab caps the key list per request.
        _CHUNK = 250
        for i in range(0, len(self.streamed_symbols), _CHUNK):
            keys = self.streamed_symbols[i : i + _CHUNK]
            request = stream.chart_equity(keys, "0,1,2,3,4,5,6,7,8")
            self._expect_base_request("CHART_EQUITY", keys, request)
            stream.send(request)
        for i in range(0, len(self.quote_covered_symbols), _CHUNK):
            keys = self.quote_covered_symbols[i : i + _CHUNK]
            request = stream.level_one_equities(keys, "0,1,2,3,4,5,8,9,10,11,34,35,37,38")
            self._expect_base_request("LEVELONE_EQUITIES", keys, request)
            stream.send(request)
        log.info("Schwab: %d symbols on real bars, %d on streamed quotes, %d on polled quotes",
                 len(self.streamed_symbols), len(self.quote_streamed_symbols), len(self.polled_symbols))
        if synthetic and rest:
            print(f"       Schwab coverage: {len(self.streamed_symbols):,} symbols on real 1-minute bars, "
                  f"{len(self.quote_streamed_symbols):,} on bars built from live quotes, "
                  f"{len(self.polled_symbols):,} on bars built from quotes polled every "
                  f"{self.POLL_SECONDS:g}s (high/low approximate). SCHWAB_SYNTHETIC_BARS=0 turns "
                  f"the last two off.", flush=True)

        def _poll() -> None:
            while not self._stop_evt.is_set():
                t0 = time.monotonic()
                with tiers_lock:
                    todo = list(self.polled_symbols)
                for i in range(0, len(todo), self.QUOTES_PER_REQUEST):
                    if self._stop_evt.is_set():
                        return
                    batch = todo[i : i + self.QUOTES_PER_REQUEST]
                    try:
                        resp = self._request(lambda b=batch: self._client.quotes(symbols=b, fields="quote"))
                        for sym, payload in (resp.json() or {}).items():
                            q = (payload or {}).get("quote") or {}
                            builder.on_quote(sym, last=_num(q.get("lastPrice")), total_volume=_num(q.get("totalVolume")),
                                             day_high=_num(q.get("highPrice")), day_low=_num(q.get("lowPrice")),
                                             last_size=_num(q.get("lastSize")))
                            self.quote_book.ingest(sym, {
                                side: (q.get(price_key), q.get(time_key), q.get(size_key))
                                for side, price_key, time_key, size_key in (
                                    ("bid", "bidPrice", "bidTime", "bidSize"),
                                    ("ask", "askPrice", "askTime", "askSize"),
                                    ("last", "lastPrice", "tradeTime", "lastSize"))
                                if price_key in q
                            }, source="schwab_rest", tier="poll", delayed=payload.get("delayed"))
                    except Exception as exc:
                        log.warning("Schwab quote poll failed for %d symbols: %s", len(batch), exc)
                self._stop_evt.wait(max(0.5, self.POLL_SECONDS - (time.monotonic() - t0)))

        def _flush() -> None:
            while not self._stop_evt.wait(1.0):
                try:
                    builder.flush()
                except Exception as exc:
                    log.error("quote bar flush failed: %s", exc, exc_info=True)

        if synthetic and rest:
            threading.Thread(target=_flush, daemon=True, name="schwab-quote-bars").start()
            threading.Thread(target=_poll, daemon=True, name="schwab-quote-poll").start()

        was_active = None
        while not self._stop_evt.wait(1.0):
            active = bool(getattr(stream, "active", False))
            if active != was_active:
                self.quote_book.connection(active)
                self.screener_book.connection(active)
                self._dynamic_connection_changed(active)
                was_active = active

    def _send_screener_requests(self, stream, keys: list[str], fields: str, command: str) -> None:
        """Send one request per list so acknowledgements retain list identity."""
        for key in keys:
            request = stream.screener_equity([key], fields, command=command)
            if isinstance(request, dict):
                self.screener_book.expect(request.get("requestid"), [key])
            stream.send(request)

    def _expect_base_request(self, service: str, keys: list[str], request) -> None:
        request_id = self._request_id(request)
        if request_id is None:
            return
        with self._dynamic_lock:
            self._base_tracking[service] = True
            self._base_requests[request_id] = (service, list(keys))
            self._dynamic_health[service] = {"status": "restoring", "error": None}

    def _ingest_base_responses(self, raw) -> dict[str, list[str]]:
        """Resolve exact accepted/discarded members for initial subscription chunks."""
        try:
            message = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (TypeError, ValueError):
            return {}
        rejected_by_service: dict[str, list[str]] = {}
        for response in (message or {}).get("response", []) or []:
            if not isinstance(response, dict):
                continue
            request_id = str(response.get("requestid"))
            with self._dynamic_lock:
                pending = self._base_requests.pop(request_id, None)
                if pending is None or pending[0] != response.get("service"):
                    continue
                service, keys = pending
                content = response.get("content") or {}
                code = content.get("code")
                discarded = 0
                if code == 19:
                    match = re.search(r"DISCARDED=(\d+)", str(content.get("msg") or ""))
                    discarded = min(len(keys), int(match.group(1))) if match else len(keys)
                elif code != 0:
                    discarded = len(keys)
                accepted = keys[:len(keys) - discarded] if discarded else keys
                rejected = keys[len(keys) - discarded:] if discarded else []
                self._base_acknowledged[service].update(accepted)
                self._base_rejected[service].update(rejected)
                self._base_rejected[service].difference_update(accepted)
                rejected_by_service[service] = rejected
                if rejected:
                    error = str(content.get("msg") or f"Schwab {service} response {code}")
                    self._dynamic_health[service] = {"status": "degraded", "error": error}
                else:
                    pending_service = any(svc == service for svc, _ in self._base_requests.values())
                    pending_service = pending_service or any(
                        item[1] == service for item in self._dynamic_requests.values())
                    self._dynamic_health[service] = {
                        "status": "restoring" if pending_service else "live", "error": None}
        return rejected_by_service

    def watch_screeners(self, keys: list[str]) -> dict:
        """Replace SCREENER_EQUITY keys without touching Chart or Level One."""
        from scanner.schwab_screener import validate_screener_key
        if not isinstance(keys, list) or not keys or len(keys) > 50:
            raise ValueError("screener list must contain 1 to 50 keys")
        normalized = list(dict.fromkeys(validate_screener_key(key) for key in keys))
        with self._screener_lock:
            stream = self._stream
            if stream is None:
                raise ValueError("Schwab stream is not started")
            old = set(self._screener_keys)
            add, remove = sorted(set(normalized) - old), sorted(old - set(normalized))
            if add:
                self._send_screener_requests(stream, add, "0,1,2,3,4", "ADD")
            if remove:
                self._send_screener_requests(stream, remove, "0", "UNSUBS")
            self._screener_keys = normalized
            self.screener_book.requested(normalized)
        return self.screener_book.snapshot()

    def set_dynamic_controller(self, controller) -> None:
        """Attach the session-only promotion controller to this stream owner."""
        with self._dynamic_lock:
            self._dynamic_controller = controller

    def dynamic_capacity(self) -> dict:
        with self._dynamic_lock, self._quote_lock:
            chart_cap = int(self._dynamic_caps["CHART_EQUITY"])
            level_cap = int(self._dynamic_caps["LEVELONE_EQUITIES"])
            chart_requested = sum(
                row.get("chart") in {"requested", "acknowledged"}
                and symbol not in self.streamed_symbols
                for symbol, row in self._dynamic_memberships.items()
            )
            level_requested = sum(
                row.get("level_one") in {"requested", "acknowledged"}
                and symbol not in self.quote_covered_symbols
                for symbol, row in self._dynamic_memberships.items()
            )
            chart_used = len(set(self.streamed_symbols)) + chart_requested
            level_used = len(set(self.quote_covered_symbols)) + level_requested
            protected = set(getattr(self._dynamic_controller, "protected_symbols", lambda: set())())
            chart_ack = (len(self._base_acknowledged["CHART_EQUITY"])
                         if self._base_tracking["CHART_EQUITY"] else len(set(self.streamed_symbols)))
            chart_ack += sum(row.get("chart") == "acknowledged"
                             and symbol not in self._base_acknowledged["CHART_EQUITY"]
                             for symbol, row in self._dynamic_memberships.items())
            level_ack = (len(self._base_acknowledged["LEVELONE_EQUITIES"])
                         if self._base_tracking["LEVELONE_EQUITIES"] else len(set(self.quote_covered_symbols)))
            level_ack += sum(row.get("level_one") == "acknowledged"
                             and symbol not in self._base_acknowledged["LEVELONE_EQUITIES"]
                             for symbol, row in self._dynamic_memberships.items())
            chart_deficit = max(0, chart_used + self._dynamic_chart_headroom - chart_cap)
            level_deficit = max(0, level_used - level_cap)
            service_blocked = any(
                self._dynamic_health[service]["status"] != "live"
                for service in ("CHART_EQUITY", "LEVELONE_EQUITIES")
            )
            return {
                "chart": {"used": chart_used, "cap": chart_cap,
                          "headroom": self._dynamic_chart_headroom,
                          "available": max(0, chart_cap - chart_used - self._dynamic_chart_headroom),
                          "deficit": chart_deficit,
                          "status": ("degraded" if chart_deficit else self._dynamic_health["CHART_EQUITY"]["status"]),
                          "error": self._dynamic_health["CHART_EQUITY"]["error"],
                          "acknowledged": chart_ack,
                          "rejected": len(self._base_rejected["CHART_EQUITY"])},
                "level_one": {"used": level_used, "cap": level_cap,
                              "headroom": 0, "available": max(0, level_cap - level_used),
                              "deficit": level_deficit,
                              "status": ("degraded" if level_deficit else self._dynamic_health["LEVELONE_EQUITIES"]["status"]),
                              "error": self._dynamic_health["LEVELONE_EQUITIES"]["error"],
                              "acknowledged": level_ack,
                              "rejected": len(self._base_rejected["LEVELONE_EQUITIES"])},
                "connection_epoch": self._dynamic_connection_epoch,
                "connected": self._dynamic_connected,
                "protected_dynamic": len(protected),
                "admissions_blocked": bool(chart_deficit or level_deficit or not self._dynamic_connected
                                           or service_blocked),
            }

    def dynamic_membership(self, symbol: str) -> dict:
        symbol = str(symbol).upper()
        with self._dynamic_lock:
            row = dict(self._dynamic_memberships.get(symbol) or {
                "symbol": symbol, "chart": "not_requested",
                "level_one": "not_requested", "error": None,
            })
        row["capacity"] = self.dynamic_capacity()
        return row

    @staticmethod
    def _request_id(request) -> str | None:
        if isinstance(request, dict):
            value = request.get("requestid")
            return str(value) if value is not None else None
        return None

    def request_dynamic_membership(self, symbol: str) -> dict:
        """Request Chart and Level One ADDs on the existing WebSocket."""
        import re
        symbol = str(symbol).upper().strip()
        if re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol) is None:
            raise ValueError("unsupported equity symbol")
        with self._dynamic_lock:
            if symbol in self.streamed_symbols or symbol in self.quote_streamed_symbols:
                raise ValueError("symbol is already in the live universe")
            capacity = self.dynamic_capacity()
            if capacity.get("admissions_blocked"):
                raise ValueError("dynamic admissions are blocked while Schwab services are not healthy")
            if capacity["chart"]["available"] < 1:
                raise ValueError("Schwab Chart Equity capacity has no admission slot after headroom")
            if capacity["level_one"]["available"] < 1:
                raise ValueError("Schwab Level One capacity is exhausted")
            stream = self._stream
            if stream is None or not bool(getattr(stream, "active", False)):
                raise ValueError("Schwab stream is not active")
            row = self._dynamic_memberships[symbol] = {
                "symbol": symbol, "chart": "requested", "level_one": "requested",
                "error": None, "desired_chart": True, "desired_level_one": True,
                "connection_epoch": self._dynamic_connection_epoch,
            }
            chart = stream.chart_equity([symbol], "0,1,2,3,4,5,6,7,8", command="ADD")
            level = stream.level_one_equities(
                [symbol], "0,1,2,3,4,5,8,9,10,11,34,35,37,38", command="ADD")
            for service, request in (("CHART_EQUITY", chart), ("LEVELONE_EQUITIES", level)):
                request_id = self._request_id(request)
                if request_id is None:
                    self._dynamic_memberships.pop(symbol, None)
                    raise ValueError(f"{service} ADD did not provide a request id")
                self._dynamic_requests[request_id] = (symbol, service, "ADD")
            stream.send(chart)
            stream.send(level)
            self.quote_book.cover(symbol)
            return dict(row)

    def is_external_quote_watch(self, symbol: str) -> bool:
        with self._quote_lock:
            return str(symbol).upper() in self._quote_watched_symbols

    def request_dynamic_release(self, symbol: str) -> dict:
        """Request service-specific UNSUBS without touching unrelated membership."""
        symbol = str(symbol).upper().strip()
        with self._dynamic_lock:
            row = self._dynamic_memberships.get(symbol)
            if row is None:
                raise ValueError("symbol has no dynamic provider membership")
            stream = self._stream
            if stream is None or not bool(getattr(stream, "active", False)):
                raise ValueError("Schwab stream is not active")
            row.update({"chart": "removal_requested", "level_one": "removal_requested",
                        "error": None, "desired_chart": False, "desired_level_one": False,
                        "connection_epoch": self._dynamic_connection_epoch})
            chart = stream.chart_equity([symbol], "0", command="UNSUBS")
            level = stream.level_one_equities([symbol], "0", command="UNSUBS")
            for service, request in (("CHART_EQUITY", chart), ("LEVELONE_EQUITIES", level)):
                request_id = self._request_id(request)
                if request_id is None:
                    raise ValueError(f"{service} UNSUBS did not provide a request id")
                self._dynamic_requests[request_id] = (symbol, service, "UNSUBS")
            stream.send(chart)
            stream.send(level)
            return dict(row)

    def _ingest_dynamic_responses(self, raw) -> None:
        try:
            message = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        except (TypeError, ValueError):
            return
        notifications: list[tuple[str, dict]] = []
        for response in (message or {}).get("response", []) or []:
            if not isinstance(response, dict):
                continue
            service = str(response.get("service") or "")
            request_id = str(response.get("requestid"))
            with self._dynamic_lock:
                pending = self._dynamic_requests.pop(request_id, None)
                if pending is None or pending[1] != service:
                    continue
                symbol, _, command = pending
                row = self._dynamic_memberships.get(symbol)
                if row is None:
                    continue
                content = response.get("content") or {}
                code = content.get("code")
                key = "chart" if service == "CHART_EQUITY" else "level_one"
                if code == 0:
                    pending_service = any(item[1] == service for item in self._dynamic_requests.values())
                    pending_service = pending_service or any(
                        item[0] == service for item in self._base_requests.values())
                    self._dynamic_health[service] = {
                        "status": "restoring" if pending_service else "live", "error": None}
                    row[key] = "removed" if command == "UNSUBS" else "acknowledged"
                    if command == "UNSUBS" and service == "CHART_EQUITY":
                        self.streamed_symbols = [item for item in self.streamed_symbols if item != symbol]
                    elif command == "UNSUBS" and service == "LEVELONE_EQUITIES":
                        with self._quote_lock:
                            self.quote_covered_symbols = [item for item in self.quote_covered_symbols if item != symbol]
                            self.quote_book.coverage(symbol, "not_watched")
                    elif service == "CHART_EQUITY" and symbol not in self.streamed_symbols:
                        self.streamed_symbols.append(symbol)
                    elif service == "LEVELONE_EQUITIES":
                        with self._quote_lock:
                            if symbol not in self.quote_covered_symbols:
                                self.quote_covered_symbols.append(symbol)
                            self.quote_book.coverage(symbol, "subscribing")
                else:
                    row[key] = "removal_rejected" if command == "UNSUBS" else "rejected"
                    row["error"] = str(content.get("msg") or f"Schwab {service} response {code}")
                    self._dynamic_health[service] = {"status": "degraded", "error": row["error"]}
                    cap = self.parse_symbol_cap(message, service)
                    if cap is not None:
                        self._dynamic_caps[service] = cap
                notifications.append((symbol, dict(row)))
        controller = self._dynamic_controller
        if controller is not None:
            for symbol, snapshot in notifications:
                controller.on_membership(symbol, snapshot)

    def _dynamic_connection_changed(self, active: bool) -> None:
        """Invalidate or restore dynamic service membership on stream transitions."""
        notifications: list[tuple[str, dict]] = []
        restore = False
        with self._dynamic_lock:
            active = bool(active)
            if active == self._dynamic_connected and self._dynamic_ever_connected:
                return
            prior = self._dynamic_connected
            self._dynamic_connected = active
            if active:
                restore = self._dynamic_ever_connected and not prior
                self._dynamic_ever_connected = True
                for service in self._dynamic_health:
                    self._dynamic_health[service] = {
                        "status": ("restoring" if restore or any(
                            item[0] == service for item in self._base_requests.values())
                            or any(item[1] == service for item in self._dynamic_requests.values())
                            else "live"), "error": None}
            else:
                if prior or self._dynamic_ever_connected:
                    self._dynamic_connection_epoch += 1
                self._dynamic_ever_connected = True
                self._dynamic_requests.clear()
                self._base_requests.clear()
                for service in self._base_acknowledged:
                    self._base_acknowledged[service].clear()
                    self._base_rejected[service].clear()
                for service in self._dynamic_health:
                    self._dynamic_health[service] = {"status": "reconnecting", "error": None}
                for symbol, row in self._dynamic_memberships.items():
                    if row.get("chart") == "removed" and row.get("level_one") == "removed":
                        continue
                    row.update({"chart": "reconnecting", "level_one": "reconnecting",
                                "error": None, "connection_epoch": self._dynamic_connection_epoch})
                    notifications.append((symbol, dict(row)))
        controller = self._dynamic_controller
        if controller is not None and not active:
            controller.on_connection(False, self._dynamic_connection_epoch)
        if restore:
            self._restore_base_memberships()
            with self._screener_lock:
                screener_keys = list(self._screener_keys)
            self.screener_book.requested(screener_keys)
            self._send_screener_requests(self._stream, screener_keys, "0,1,2,3,4", "ADD")
            self._restore_dynamic_memberships()

    def _restore_base_memberships(self) -> None:
        """Reassert fixed/startup and external-watch membership after reconnect."""
        with self._dynamic_lock, self._quote_lock:
            stream = self._stream
            if stream is None or not bool(getattr(stream, "active", False)):
                return
            dynamic = set(self._dynamic_memberships)
            chart_symbols = [symbol for symbol in self.streamed_symbols if symbol not in dynamic]
            quote_symbols = [symbol for symbol in self.quote_covered_symbols if symbol not in dynamic]
            for service in self._base_tracking:
                self._base_tracking[service] = False
            for i in range(0, len(chart_symbols), 250):
                keys = chart_symbols[i:i + 250]
                request = stream.chart_equity(keys, "0,1,2,3,4,5,6,7,8", command="ADD")
                self._expect_base_request("CHART_EQUITY", keys, request)
                stream.send(request)
            for i in range(0, len(quote_symbols), 250):
                keys = quote_symbols[i:i + 250]
                request = stream.level_one_equities(
                    keys, "0,1,2,3,4,5,8,9,10,11,34,35,37,38", command="ADD")
                self._expect_base_request("LEVELONE_EQUITIES", keys, request)
                stream.send(request)

    def _restore_dynamic_memberships(self) -> None:
        """Reassert desired dynamic services on the replacement stream."""
        with self._dynamic_lock:
            stream = self._stream
            if stream is None or not bool(getattr(stream, "active", False)):
                return
            rows = [(symbol, dict(row)) for symbol, row in self._dynamic_memberships.items()
                    if row.get("chart") == "reconnecting" or row.get("level_one") == "reconnecting"]
            for symbol, snapshot in rows:
                add = bool(snapshot.get("desired_chart") or snapshot.get("desired_level_one"))
                command = "ADD" if add else "UNSUBS"
                fields = ("0,1,2,3,4,5,6,7,8" if add else "0")
                quote_fields = ("0,1,2,3,4,5,8,9,10,11,34,35,37,38" if add else "0")
                chart = stream.chart_equity([symbol], fields, command=command)
                level = stream.level_one_equities([symbol], quote_fields, command=command)
                row = self._dynamic_memberships[symbol]
                row.update({"chart": "requested" if add else "removal_requested",
                            "level_one": "requested" if add else "removal_requested",
                            "connection_epoch": self._dynamic_connection_epoch})
                for service, request in (("CHART_EQUITY", chart), ("LEVELONE_EQUITIES", level)):
                    request_id = self._request_id(request)
                    if request_id is None:
                        key = "chart" if service == "CHART_EQUITY" else "level_one"
                        row[key] = "unavailable"
                        row["error"] = f"{service} {command} restore did not provide a request id"
                        self._dynamic_health[service] = {"status": "unavailable", "error": row["error"]}
                        continue
                    self._dynamic_requests[request_id] = (symbol, service, command)
                    stream.send(request)
                controller = self._dynamic_controller
                if controller is not None:
                    controller.on_membership(symbol, dict(row))

    def watch_quotes(self, symbols: list[str]) -> dict:
        """Replace external held-symbol watch set on the existing Schwab stream.

        Callers should refresh their complete held-symbol set when it changes.
        A maximum of 64 extra symbols prevents accidental provider-budget loss.
        """
        import re
        normalized = [str(s).upper().strip() for s in symbols]
        if len(normalized) > 64 or any(not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", s) for s in normalized):
            raise ValueError("watch list must contain at most 64 valid equity symbols")
        requested = set(normalized)
        with self._quote_lock:
            old = self._quote_watched_symbols
            add = sorted(requested - old - set(self.quote_covered_symbols))
            remove = sorted(old - requested - self._quote_base_symbols)
            if len(self.quote_covered_symbols) + len(add) > self.LEVELONE_CAP:
                raise ValueError("Schwab Level One symbol budget exhausted")
            stream = self._stream
            if stream is None:
                raise ValueError("Schwab stream is not started")
            fields = "0,1,2,3,4,5,8,9,10,11,34,35,37,38"
            if add:
                stream.send(stream.level_one_equities(add, fields, command="ADD"))
            if remove:
                stream.send(stream.level_one_equities(remove, "0", command="UNSUBS"))
            self._quote_watched_symbols = requested
            self.quote_covered_symbols = [s for s in self.quote_covered_symbols if s not in remove] + add
            for sym in add:
                self.quote_book.cover(sym)
            for sym in remove:
                self.quote_book.coverage(sym, "not_watched")
            return {"watched": sorted(requested), "stream_covered": sorted(requested & set(self.quote_covered_symbols)),
                    "budget_used": len(self.quote_covered_symbols), "budget_cap": self.LEVELONE_CAP}

    def _warn_cap(self, cap: int) -> None:
        n, total = len(self.unstreamed_symbols), len(self.unstreamed_symbols) + len(self.streamed_symbols)
        text = (f"Schwab streams live 1-minute bars for at most {cap} symbols per account, and "
                f"SCHWAB_SYNTHETIC_BARS is off. {n:,} of your {total:,} symbols are NOT being scanned "
                f"live: only the first {cap} in the universe file (the most liquid) are.")
        log.error(text)
        print("\n" + "!" * 72 + f"\n  {text}\n" + "!" * 72 + "\n", flush=True)

    def stop_stream(self) -> None:
        self._stop_evt.set()
        if self._stream is not None:
            try:
                self._stream.stop()
            except Exception as exc:
                log.debug("Schwab stream stop: %s", exc)
            log.info("Schwab stream stopped")
