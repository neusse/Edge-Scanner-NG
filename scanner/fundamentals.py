"""Day-cached fundamentals for the Dashboard V2 Stock Info window.

Everything here is fail-open and off the request thread:
  * `FundamentalsCache.get(sym)` returns a good entry for FRESH_DAYS (name, sector,
    float and the business summary barely move; an earnings date that has passed
    makes it stale at once). A failure is kept for the day, except a Yahoo rate
    limit, which is never cached: that symbol is simply fetched again later.
  * `request(sym)` enqueues a background fetch (deduped) and returns at once;
    the route answers `{pending: true}` until the worker lands the entry.
  * `prefetch_all` / `start_background_prefetch` warm the whole universe in
    a daemon thread after the scanner's warmup (run_live.py, `--no-fundamentals`
    to skip). Yahoo rate-limits a burst of a few hundred lookups, so every Yahoo fetch
    waits out a shared cool-down after a rate limit (60 s, doubling to 15 min).
    Refetching the whole universe every morning used to hit that limit after
    about a thousand symbols and blank the rest for the day.

Cache file: data/fundamentals.json -> {"built": "YYYY-MM-DD", "symbols": {SYM: {...}}}.
`data/` is gitignored.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from scanner.json_store import AtomicJsonStore

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
DEFAULT_PATH = Path("data/fundamentals.json")
_FLUSH_EVERY = 25
FRESH_DAYS = 7                 # a good entry is reused this long
_COOLDOWN_MIN_S = 60.0         # first wait after a rate limit
_COOLDOWN_MAX_S = 900.0        # longest wait (doubles up to this)

# yfinance is chatty (one ERROR line per symbol with no data); the dashboard
# does not care and the scanner console must stay readable.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)
logging.getLogger("peewee").setLevel(logging.CRITICAL)


def _today_et() -> str:
    return datetime.now(_ET).strftime("%Y-%m-%d")


def _num(v) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return f if f == f and f not in (float("inf"), float("-inf")) else None
    except (TypeError, ValueError):
        return None


def _is_rate_limit(exc: BaseException) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    return "ratelimit" in text or "rate limit" in text or "too many requests" in text or "429" in text


def _default_ticker_factory(symbol: str):
    import yfinance
    return yfinance.Ticker(symbol)


def _next_earnings(ticker, now: datetime) -> Optional[str]:
    """First earnings date strictly in the future from `get_earnings_dates(limit=8)`."""
    try:
        df = ticker.get_earnings_dates(limit=8)
    except Exception:
        return None
    if df is None or getattr(df, "empty", True):
        return None
    try:
        import pandas as pd
        idx = pd.to_datetime(df.index)
        if getattr(idx, "tz", None) is None:
            idx = idx.tz_localize(_ET)
        future = [ts for ts in idx if ts.to_pydatetime() > now]
        if not future:
            return None
        return min(future).tz_convert(_ET).strftime("%Y-%m-%d")
    except Exception:
        return None


class FundamentalsCache:
    def __init__(
        self,
        path: Path = DEFAULT_PATH,
        *,
        ticker_factory: Optional[Callable[[str], object]] = None,
        batch_fetcher: Optional[Callable[[list[str]], dict[str, dict]]] = None,
        provider_name: Optional[str] = None,
    ) -> None:
        self._store = AtomicJsonStore(Path(path))
        self._factory = ticker_factory or _default_ticker_factory
        self._batch_fetcher = batch_fetcher
        self._provider_name = provider_name
        self._lock = threading.Lock()
        self._symbols: dict[str, dict] = {}
        self._dirty = False
        self._queue: queue.Queue[str] = queue.Queue()
        self._pending: set[str] = set()
        self._worker: Optional[threading.Thread] = None
        # shared rate-limit cool-down: no fetch starts before this monotonic time
        self._cool_until = 0.0
        self._cool_s = _COOLDOWN_MIN_S
        self._load()

    def configure_provider(
        self,
        batch_fetcher: Optional[Callable[[list[str]], dict[str, dict]]],
        provider_name: Optional[str],
    ) -> None:
        """Attach the active feed's optional batched fundamentals capability."""
        with self._lock:
            self._batch_fetcher = batch_fetcher
            self._provider_name = provider_name

    # ── disk ────────────────────────────────────────────────────────────────

    def _load(self) -> None:
        try:
            doc = self._store.read()
            if isinstance(doc, dict) and isinstance(doc.get("symbols"), dict):
                self._symbols = {k: v for k, v in doc["symbols"].items() if isinstance(v, dict)}
        except Exception as exc:
            log.warning("fundamentals: load error: %s", exc)
            self._symbols = {}

    def flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            snapshot = {"built": _today_et(), "symbols": dict(self._symbols)}
            self._dirty = False
        try:
            self._store.write(snapshot)
        except Exception as exc:
            log.warning("fundamentals: flush error: %s", exc)

    # ── reads ───────────────────────────────────────────────────────────────

    def is_fresh(self, symbol: str) -> bool:
        return self.get(symbol) is not None

    def get(self, symbol: str) -> Optional[dict]:
        sym = symbol.upper().strip()
        with self._lock:
            entry = self._symbols.get(sym)
        if not entry:
            return None
        if "website" not in entry:          # entry predates the website / summary fields: refetch
            return None
        if self._provider_name and entry.get("provider") != self._provider_name:
            return None
        # A rate limit is never a real answer: fetch again. Entries written before the
        # flag existed carry only the error text.
        if entry.get("rate_limited") or "rate limit" in str(entry.get("error") or "").lower():
            return None
        fetched = str(entry.get("fetched_at", ""))[:10]
        today = _today_et()
        if not entry.get("ok"):
            return dict(entry) if fetched == today else None
        try:
            age = (datetime.strptime(today, "%Y-%m-%d") - datetime.strptime(fetched, "%Y-%m-%d")).days
        except ValueError:
            return None
        if age < 0 or age >= FRESH_DAYS:
            return None
        nxt = entry.get("next_earnings")
        if nxt and str(nxt) < today:        # the earnings date it knows has passed
            return None
        return dict(entry)

    def _put(self, symbol: str, entry: dict) -> None:
        with self._lock:
            self._symbols[symbol] = entry
            self._dirty = True

    # ── fetch ───────────────────────────────────────────────────────────────

    @staticmethod
    def _merge_provider(entry: dict, row: Optional[dict], provider_name: Optional[str]) -> None:
        if not row or provider_name != "schwab":
            return
        fundamental = row.get("fundamental") if isinstance(row.get("fundamental"), dict) else {}
        short_pct = _num(fundamental.get("shortIntToFloat"))
        entry.update({
            "ok": True,
            "provider": "schwab",
            "name": entry.get("name") or row.get("description"),
            "market_cap": _num(fundamental.get("marketCap")),
            "shares_outstanding": _num(fundamental.get("sharesOutstanding")),
            # Schwab's oddly named marketCapFloat behaves as float shares.
            "float_shares": _num(fundamental.get("marketCapFloat")),
            # Schwab supplies this as percentage points; the existing API uses a fraction.
            "short_pct_float": short_pct / 100 if short_pct is not None else None,
            "short_ratio": _num(fundamental.get("shortIntDayToCover")),
            "schwab_instrument": {
                key: row.get(key) for key in
                ("assetType", "assetSubType", "cusip", "description", "exchange")
                if row.get(key) is not None
            },
            "schwab_fundamentals": dict(fundamental),
        })
        entry.pop("error", None)
        entry.pop("rate_limited", None)

    def fetch_one(self, symbol: str, *, provider_row: Optional[dict] = None,
                  fetch_provider: bool = True) -> dict:
        """Fetch one symbol synchronously. Never raises; failures come back as ok=False."""
        sym = symbol.upper().strip()
        if fetch_provider and self._batch_fetcher is not None:
            try:
                provider_row = self._batch_fetcher([sym]).get(sym)
            except Exception as exc:
                log.debug("fundamentals provider: %s failed: %s", sym, exc)
        self._wait_cooldown()
        now = datetime.now(_ET)
        entry: dict = {
            "symbol": sym, "fetched_at": now.isoformat(timespec="seconds"), "ok": False,
            "name": None, "sector": None, "industry": None, "market_cap": None,
            "shares_outstanding": None, "float_shares": None, "short_pct_float": None,
            "short_ratio": None, "next_earnings": None, "website": None, "summary": None,
        }
        try:
            t = self._factory(sym)
            info = t.info or {}
            if not isinstance(info, dict):
                info = {}
            entry.update({
                "ok": True,
                "name": info.get("longName") or info.get("shortName"),
                "sector": info.get("sector"),
                "industry": info.get("industry"),
                "market_cap": _num(info.get("marketCap")),
                "shares_outstanding": _num(info.get("sharesOutstanding")),
                "float_shares": _num(info.get("floatShares")),
                "short_pct_float": _num(info.get("shortPercentOfFloat")),
                "short_ratio": _num(info.get("shortRatio")),
                "website": info.get("website") or None,
                "summary": (str(info.get("longBusinessSummary") or "")[:1500] or None),
            })
            entry["next_earnings"] = _next_earnings(t, now)
            self._cool_s = _COOLDOWN_MIN_S
        except Exception as exc:
            entry["ok"] = False
            entry["error"] = str(exc)[:200]
            if _is_rate_limit(exc):
                entry["rate_limited"] = True
                self._start_cooldown()
                with self._lock:
                    old = self._symbols.get(sym)
                if old and old.get("ok") and provider_row is None:
                    return dict(old)        # keep the last good answer rather than blank it
            log.debug("fundamentals: %s failed: %s", sym, exc)
        self._merge_provider(entry, provider_row, self._provider_name)
        self._put(sym, entry)
        return entry

    def _start_cooldown(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._cool_until:      # another thread already started this one
                return
            self._cool_until = now + self._cool_s
            log.info("fundamentals: Yahoo rate limit, pausing %.0f s", self._cool_s)
            self._cool_s = min(self._cool_s * 2, _COOLDOWN_MAX_S)

    def _wait_cooldown(self) -> None:
        while True:
            with self._lock:
                left = self._cool_until - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 5.0))

    def prefetch_all(self, symbols: list[str], workers: int = 3) -> int:
        """Fetch every symbol without a fresh entry. Returns the number fetched."""
        todo = [s.upper().strip() for s in symbols if s and not self.is_fresh(s)]
        if not todo:
            return 0
        provider_rows: dict[str, dict] = {}
        if self._batch_fetcher is not None:
            try:
                provider_rows = self._batch_fetcher(todo)
            except Exception as exc:
                log.warning("fundamentals provider prefetch failed: %s", exc)
        done = 0
        try:
            with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="fund") as ex:
                futs = {
                    ex.submit(self.fetch_one, s, provider_row=provider_rows.get(s), fetch_provider=False): s
                    for s in todo
                }
                for fut in as_completed(futs):
                    done += 1
                    if done % _FLUSH_EVERY == 0:
                        self.flush()
        except Exception as exc:
            log.warning("fundamentals: prefetch error: %s", exc)
        self.flush()
        return done

    def start_background_prefetch(self, symbols: list[str], workers: int = 3) -> threading.Thread:
        def _run() -> None:
            try:
                n = self.prefetch_all(symbols, workers=workers)
                log.info("fundamentals: prefetched %d symbols", n)
            except Exception as exc:
                log.warning("fundamentals: background prefetch error: %s", exc)
        th = threading.Thread(target=_run, daemon=True, name="fundamentals-prefetch")
        th.start()
        return th

    # ── on-demand queue ─────────────────────────────────────────────────────

    def request(self, symbol: str) -> None:
        """Enqueue a background fetch for `symbol` (no-op if fresh or already queued)."""
        sym = symbol.upper().strip()
        if not sym or self.is_fresh(sym):
            return
        with self._lock:
            if sym in self._pending:
                return
            self._pending.add(sym)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._worker_loop, daemon=True,
                                                name="fundamentals-worker")
                self._worker.start()
        self._queue.put(sym)

    def _worker_loop(self) -> None:
        while True:
            sym = self._queue.get()
            try:
                self.fetch_one(sym)
                self.flush()
            except Exception as exc:
                log.debug("fundamentals worker: %s: %s", sym, exc)
            finally:
                with self._lock:
                    self._pending.discard(sym)
                self._queue.task_done()


# ── module-level default instance (shared by run_live.py and api_v2.py) ──────

_default: Optional[FundamentalsCache] = None
_default_lock = threading.Lock()


def get_cache(path: Optional[Path] = None, **kw) -> FundamentalsCache:
    """The process-wide cache for the default path; a fresh instance for any other path."""
    global _default
    if path is not None and Path(path) != DEFAULT_PATH:
        return FundamentalsCache(Path(path), **kw)
    with _default_lock:
        if _default is None:
            _default = FundamentalsCache(DEFAULT_PATH, **kw)
        return _default


def start_background_prefetch(symbols: list[str], workers: int = 3) -> threading.Thread:
    """Warm the default cache for `symbols` in a daemon thread (skips fresh symbols)."""
    return get_cache().start_background_prefetch(symbols, workers=workers)
