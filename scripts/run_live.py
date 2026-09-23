#!/usr/bin/env python
"""End-to-end live scanner.  Run this once each morning:

    python scripts/run_live.py

What it does automatically:
  1. Loads (or rebuilds) the ~900-symbol universe
  2. Builds a sector map so the sector-strength gate works
  3. Downloads / refreshes the daily price history cache
  4. Downloads / refreshes the 5-min bar cache for relative-volume
  5. Warms up the scanner with that history
  6. Connects to Alpaca live 1-min bars and scans until you press Ctrl+C

Alerts print to the screen as they fire.  Press Ctrl+C to stop.

Options:
  --refresh-universe   Force rebuild of universe even if it is fresh
  --history-days N     Days of daily history to load (default: 60)
  --intraday-days N    Days of 5-min bars to load for RVOL (default: 20)
  --log-level LEVEL    Python log level: DEBUG / INFO / WARNING (default: WARNING)
"""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import load_dotenv

# Run from the repo root regardless of the caller's cwd — all data/cache
# paths (data/universe.csv, data/daily, scripts/build_universe.py) are
# repo-relative.
_REPO_ROOT = Path(__file__).parent.parent
os.chdir(_REPO_ROOT)
sys.path.insert(0, str(_REPO_ROOT))
load_dotenv(override=True)

from scanner.api import AppState, bind_sockets, create_app
from scanner.feed_hub import FeedHub
from scanner.data import FEEDS, make_feed
from scanner.live_scanner import LiveScanner
from scanner.market import classify_market
from scanner.custom_setups import CustomEvaluator
from scanner.fundamentals import get_cache as get_fundamentals_cache
from scanner.profiles import ProfileEngine
from scanner.events import EventBuffer, make_hodlod_hook   # Dashboard V2 HOD/LOD ticker
from scanner import plugins
from scanner.alert_sink import AlertSink
from scanner.universe import load_universe
from scanner.universe_selection import (
    UniverseSelectionError, load_active_universe, validate_stream_budget,
)

log = logging.getLogger(__name__)

_DEFAULT_UNIVERSE    = Path("data/universe.csv")
_DEFAULT_SECTOR_MAP  = Path("data/sector_map.csv")
_UNIVERSE_MAX_AGE_D  = 7     # rebuild universe if older than this many days
_SECTOR_MAP_MAX_AGE_D = 7    # re-check a symbol's sector after this many days

# yfinance sector name  →  SPDR sector ETF
_SECTOR_ETF: dict[str, str] = {
    "Technology":             "XLK",
    "Financial Services":     "XLF",
    "Healthcare":             "XLV",
    "Consumer Cyclical":      "XLY",
    "Consumer Defensive":     "XLP",
    "Energy":                 "XLE",
    "Industrials":            "XLI",
    "Basic Materials":        "XLB",
    "Real Estate":            "XLRE",
    "Utilities":              "XLU",
    "Communication Services": "XLC",
}


# ─────────────────────────────────────────────────────────────────────────────
# Startup helpers
# ─────────────────────────────────────────────────────────────────────────────

def _file_age_days(p: Path) -> float:
    """Return how many days ago a file was last modified. inf if missing."""
    if not p.exists():
        return float("inf")
    return (datetime.now() - datetime.fromtimestamp(p.stat().st_mtime)).total_seconds() / 86400


def _banner(msg: str) -> None:
    width = 64
    print(f"\n{'-' * width}")
    print(f"  {msg}")
    print(f"{'-' * width}")


def _step(n: int, total: int, msg: str) -> None:
    print(f"[{n}/{total}] {msg}", flush=True)


# ─────────────────────────────────────────────────────────────────────────────
# Step 1: Universe
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_universe(force_refresh: bool, path: Path = _DEFAULT_UNIVERSE, provider: str = "alpaca") -> list[str]:
    # An explicitly named universe file is used as-is. It was built deliberately
    # with its own thresholds, so the age check and auto-rebuild (which would
    # regenerate it with the DEFAULT thresholds) must not apply to it.
    if path != _DEFAULT_UNIVERSE:
        symbols = load_universe(path)
        print(f"       {len(symbols)} symbols loaded from {path}")
        return symbols
    age = _file_age_days(path)
    if force_refresh or age > _UNIVERSE_MAX_AGE_D:
        reason = "forced" if force_refresh else f"{age:.0f} days old"
        print(f"       Universe is {reason} --rebuilding (takes ~10s) ...", flush=True)
        import subprocess
        result = subprocess.run(
            # rebuilt with the same provider the session runs on
            [sys.executable, "scripts/build_universe.py", "--provider", provider],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print("       ERROR rebuilding universe:")
            print(result.stderr[-2000:])
            sys.exit(1)
        # Print the summary line from the script
        for line in result.stdout.splitlines():
            if line.strip():
                print(f"       {line}")

    symbols = load_universe(path)
    print(f"       {len(symbols)} symbols loaded from {path}")
    return symbols


# ─────────────────────────────────────────────────────────────────────────────
# Step 2: Sector map
# ─────────────────────────────────────────────────────────────────────────────

def _get_sector_yf(sym: str) -> tuple[str, Optional[str], bool]:
    """Sector for one symbol via yfinance: (sym, sector or None, lookup_ok).
    lookup_ok is False on an error, so a failed lookup is retried next start
    instead of being remembered as "no sector"."""
    try:
        import yfinance as yf
        return sym, yf.Ticker(sym).info.get("sector"), True
    except Exception:
        return sym, None, False


# Yahoo rate-limits bursts, so each start looks up at most this many symbols
# (about 2 minutes). A large new universe fills in over a few starts; the
# symbols already mapped are used meanwhile.
_SECTOR_LOOKUPS_PER_START = 1500


def _load_sector_rows() -> dict[str, dict]:
    """{symbol: {"sector_etf": str, "checked": "YYYY-MM-DD"}} from the map file.
    An empty sector_etf means "looked up, has no sector" (funds, units)."""
    if not _DEFAULT_SECTOR_MAP.exists():
        return {}
    df = pd.read_csv(_DEFAULT_SECTOR_MAP, dtype=str, keep_default_na=False)
    if "checked" not in df.columns:                  # older files: date = file date
        df["checked"] = datetime.fromtimestamp(_DEFAULT_SECTOR_MAP.stat().st_mtime).strftime("%Y-%m-%d")
    return {r.symbol: {"sector_etf": r.sector_etf, "checked": r.checked} for r in df.itertuples()}


def _ensure_sector_map(symbols: list[str]) -> tuple[dict[str, str], list[str]]:
    """Load the sector map and top it up. Symbols never looked up come first,
    then entries older than _SECTOR_MAP_MAX_AGE_D, capped per start.
    Returns (sector_map for the universe, list_of_sector_etfs)."""
    rows = _load_sector_rows()
    today = date.today()
    stale_before = (today - timedelta(days=_SECTOR_MAP_MAX_AGE_D)).isoformat()
    missing = [s for s in symbols if s not in rows]
    stale = [s for s in symbols if s in rows and rows[s]["checked"] < stale_before]
    # Warrants, rights and units (5-letter tickers ending W / R / U) have no
    # sector; look them up last so real stocks get mapped first.
    derivative = lambda sym: len(sym) == 5 and sym[-1] in "WRU"
    missing.sort(key=derivative)
    todo = (missing + stale)[:_SECTOR_LOOKUPS_PER_START]
    if todo:
        left = len(missing) + len(stale) - len(todo)
        print(f"       Looking up sectors for {len(todo)} symbols ({len(missing)} new, {len(stale)} due for "
              f"refresh{f', {left} left for later starts' if left else ''}) ...", flush=True)
        # Few workers and a circuit breaker: once Yahoo starts refusing (it
        # rate-limits bursts), every further request fails too, so stop and
        # leave the rest for the next start instead of burning through them.
        done = failed = 0
        recent: list[bool] = []
        with ThreadPoolExecutor(max_workers=6) as ex:
            futs = [ex.submit(_get_sector_yf, s) for s in todo]
            for fut in as_completed(futs):
                sym, sector, ok = fut.result()
                done += 1
                recent = (recent + [ok])[-50:]
                if ok:
                    rows[sym] = {"sector_etf": _SECTOR_ETF.get(sector or "", ""), "checked": today.isoformat()}
                else:
                    failed += 1
                if done % 500 == 0 or done == len(todo):
                    print(f"       {done}/{len(todo)} looked up ({failed} failed, retried next start)", flush=True)
                if len(recent) == 50 and recent.count(False) > 25:
                    for f in futs:
                        f.cancel()
                    print(f"       Yahoo is rate-limiting; stopped after {done} lookups, the rest continue "
                          f"next start", flush=True)
                    break
        _DEFAULT_SECTOR_MAP.parent.mkdir(parents=True, exist_ok=True)
        out = pd.DataFrame([{"symbol": k, **v} for k, v in sorted(rows.items())],
                           columns=["symbol", "sector_etf", "checked"])
        out.to_csv(_DEFAULT_SECTOR_MAP, index=False)

    universe = set(symbols)
    sector_map = {s: r["sector_etf"] for s, r in rows.items() if s in universe and r["sector_etf"]}
    no_sector = sum(1 for s, r in rows.items() if s in universe and not r["sector_etf"])
    unknown = sum(1 for s in symbols if s not in rows)
    print(f"       {len(sector_map)}/{len(symbols)} symbols mapped to a sector ETF "
          f"({no_sector} have no sector, {unknown} not looked up yet)", flush=True)
    return sector_map, sorted(set(sector_map.values()))


# ─────────────────────────────────────────────────────────────────────────────
# Step 3: Daily history
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_daily_history(
    feed: AlpacaFeed,
    symbols: list[str],
    sector_etfs: list[str],
    history_days: int,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict[str, pd.DataFrame]]:
    """Download/refresh daily bars. Returns (spy_daily, symbol_daily, sector_daily)."""
    end   = date.today() - timedelta(days=1)
    start = end - timedelta(days=history_days)

    all_syms = ["SPY"] + sector_etfs + symbols
    total    = len(all_syms)

    symbol_daily: dict[str, pd.DataFrame] = {}
    sector_daily: dict[str, pd.DataFrame] = {}

    def _tick(done: int, of: int) -> None:
        if done % 250 == 0 or done == of:
            print(f"       {done}/{of} ...", flush=True)

    # Batched: one request per 500 symbols instead of one per symbol. At a few
    # hundred symbols the serial loop was fine; at a few thousand it was the
    # single biggest cost in startup.
    got = feed.get_historical_daily_multi(all_syms, start, end, progress=_tick)

    spy_daily = got.get("SPY", pd.DataFrame())
    for sym, df in got.items():
        if sym == "SPY":
            continue
        if sym in sector_etfs:
            sector_daily[sym] = df
        else:
            symbol_daily[sym] = df
    errors = total - len(got)
    if spy_daily.empty:
        # Previously spy_daily was only bound inside the loop, so a SPY failure
        # surfaced as an UnboundLocalError further down instead of this.
        sys.exit("Could not load SPY daily history; the scanner cannot start without it.")

    print(f"       Done. {total - errors}/{total} symbols cached.")
    return spy_daily, symbol_daily, sector_daily


# ─────────────────────────────────────────────────────────────────────────────
# Step 4: 5-min bars for RVOL
# ─────────────────────────────────────────────────────────────────────────────

def _fetch_intraday_history(
    feed: AlpacaFeed,
    symbols: list[str],
    intraday_days: int,
) -> dict[str, pd.DataFrame]:
    """Download/refresh 5-min bars for RVOL volume profile."""
    end   = date.today() - timedelta(days=1)
    start = end - timedelta(days=intraday_days * 2 + 5)  # 2× cushion for weekends

    total = len(symbols)

    def _tick(done: int, of: int) -> None:
        if done % 250 == 0 or done == of:
            print(f"       {done}/{of} ...", flush=True)

    # The expensive half of warmup: 20 days of 5-min bars is ~1,560 bars per
    # symbol. Batches are small (50) because of the 10,000-bar page limit.
    got = feed.get_historical_bars_multi(symbols, "5Min", start, end, progress=_tick)
    bars_5m = {s: d for s, d in got.items() if not d.empty}

    print(f"       Done. {len(bars_5m)}/{total} symbols have 5-min bars.")
    return bars_5m


# ─────────────────────────────────────────────────────────────────────────────
# Custom-setup alerts, printed as they fire
# ─────────────────────────────────────────────────────────────────────────────

class _CustomPrintSink(AlertSink):
    def push(self, alert: dict) -> bool:
        accepted = super().push(alert)
        if accepted:
            try:
                ts = pd.Timestamp(alert["timestamp"]).tz_convert("America/New_York").strftime("%H:%M ET")
            except Exception:
                ts = str(alert.get("timestamp", ""))
            print(f"  {ts}  {alert['symbol']:<6} {alert['direction'].upper():<5}  "
                  f"{alert.get('setup_label') or alert.get('setup')}  ${alert['price']:.2f}", flush=True)
        return accepted


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def _print_history_summary(feed, bucket: str, total: int, started: float) -> None:
    stats = getattr(feed, "_history_stats", {}).get(bucket)
    if stats is None:  # third-party providers may not expose cache diagnostics
        return
    counted = stats["current"] + stats["incremental"] + stats["full"]
    print(
        f"       Cache: {stats['current']} current, {stats['incremental']} incremental, "
        f"{stats['full']} full, {max(0, total - counted)} failed; "
        f"{stats['requests']} history request(s) in {time.perf_counter() - started:.1f}s",
        flush=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the live scanner end-to-end (build universe -> warmup -> scan)"
    )
    parser.add_argument("--universe", default=str(_DEFAULT_UNIVERSE),
                        help="universe CSV to scan (default: data/universe.csv). An explicit "
                             "file is used verbatim, with no age check or rebuild.")
    parser.add_argument("--refresh-universe", action="store_true",
                        help="Force universe rebuild even if it is fresh")
    parser.add_argument("--force-refresh-history", action="store_true",
                        help="Ignore daily and 5-minute history caches and download full windows")
    parser.add_argument("--history-days",  type=int, default=60,
                        help="Days of daily history to load (default: 60)")
    parser.add_argument("--intraday-days", type=int, default=20,
                        help="Days of 5-min bars for RVOL (default: 20)")
    parser.add_argument("--keep-days",    type=int, default=5,
                        help="Days of session alerts to retain on disk (default: 5)")
    _provider = (os.environ.get("DATA_PROVIDER") or "alpaca").strip().lower()
    parser.add_argument("--feed", choices=FEEDS, default=_provider if _provider in FEEDS else "alpaca",
                        help="Market data provider (default: DATA_PROVIDER in .env, else alpaca)")
    parser.add_argument("--no-fundamentals", action="store_true",
                        help="Skip the Dashboard V2 fundamentals prefetch (background "
                             "thread after warmup; never blocks scanning).")
    parser.add_argument("--port", type=int, default=7777,
                        help="Port for the dashboard, API and unified feed (default 7777). A second "
                             "scanner, for example one on another data provider, needs its own port.")
    parser.add_argument("--alerts-dir", default="data/alerts",
                        help="Folder for the alert archive (default data/alerts). Give a second scanner "
                             "its own folder so the two archives never mix.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Address the API and feeds bind to. Default is this machine only. "
                             "The API has no authentication: bind a LAN address only on a network you trust.")
    parser.add_argument("--log-level",     default="WARNING",
                        help="DEBUG / INFO / WARNING (default: WARNING)")
    ext = plugins.live_extension()          # optional engine plugin (scanner/plugins.py)
    if ext is not None:
        ext.add_args(parser)
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    )

    data_desc = args.feed.capitalize()
    if args.feed == "alpaca":
        data_desc += f" ({(os.environ.get('ALPACA_FEED') or 'sip').strip().upper()} feed)"
    _banner(f"Live Scanner  -- {date.today()}   data: {data_desc}")
    TOTAL_STEPS = 6

    # ── 1. Universe ───────────────────────────────────────────────────────────
    _step(1, TOTAL_STEPS, "Universe")
    symbols = _ensure_universe(args.refresh_universe, Path(args.universe), args.feed)
    try:
        active_universe = load_active_universe()
    except UniverseSelectionError as exc:
        sys.exit(f"\n  CANNOT START: {exc}\n")
    if active_universe is not None:
        symbols = active_universe.symbols
        print(f"       Active watchlist: {active_universe.name} ({len(symbols)} symbols)", flush=True)

    # ── 2. Sector map ─────────────────────────────────────────────────────────
    _step(2, TOTAL_STEPS, "Sector map")
    sector_map, sector_etfs = _ensure_sector_map(symbols)
    if not sector_map:
        print("\n  CANNOT START: sector map is empty.\n"
              "  The sector_rrs gate requires sector data --no alerts will fire.\n"
              "  Check your internet connection and try again.\n")
        sys.exit(1)
    if active_universe is not None and args.feed == "schwab":
        try:
            budget = validate_stream_budget(active_universe, sector_etfs, cap=300)
        except UniverseSelectionError as exc:
            sys.exit(f"\n  CANNOT START: {exc}\n")
        print(
            f"       Schwab stream budget: {budget['watchlist_count']} watchlist + "
            f"{budget['support_count']} support = {budget['total_count']}/{budget['cap']}",
            flush=True,
        )

    # ── 3. Daily history ──────────────────────────────────────────────────────
    _step(3, TOTAL_STEPS, f"Daily history  ({args.history_days} days)")
    feed = make_feed(args.feed)
    if args.force_refresh_history:
        if not hasattr(feed, "force_refresh_history"):
            sys.exit(f"--force-refresh-history is not supported by the {args.feed} feed")
        feed.force_refresh_history = True
    if args.feed != "alpaca":
        print(f"       Note: default thresholds were set on Alpaca SIP data; {args.feed} "
              f"volumes can differ slightly", flush=True)
    elif os.environ.get("ALPACA_FEED", "sip").strip().lower() == "iex":
        print("       *** ALPACA_FEED=iex: free single-exchange data. Volume and RVOL read far "
              "lower than on SIP, so volume-based setups fire much less. ***", flush=True)
    history_started = time.perf_counter()
    spy_daily, symbol_daily, sector_daily = _fetch_daily_history(
        feed, symbols, sector_etfs, args.history_days
    )
    _print_history_summary(feed, "daily", 1 + len(sector_etfs) + len(symbols), history_started)

    # ── 4. 5-min bars (RVOL) ─────────────────────────────────────────────────
    _step(4, TOTAL_STEPS, f"Intraday bars  ({args.intraday_days} days of 5-min data)")
    history_started = time.perf_counter()
    bars_5m = _fetch_intraday_history(feed, symbols, args.intraday_days)
    _print_history_summary(feed, "5m", len(symbols), history_started)
    if not bars_5m:
        print("\n  WARNING: No 5-min bars fetched.\n"
              "  The rvol gate requires intraday history --no alerts will fire.\n"
              "  Check your Alpaca credentials and try again.\n")
        sys.exit(1)

    # ── 5. Warmup ─────────────────────────────────────────────────────────────
    _step(5, TOTAL_STEPS, "Warming up scanner ...")
    scanner = LiveScanner(symbols, feed, sector_map=sector_map)
    app_state = AppState(scanner=scanner, feed=feed, keep_days=args.keep_days,
                         hub=FeedHub(store_dir=Path(args.alerts_dir) / "all", keep_days=args.keep_days,
                                     load_persisted=True))
    # Dashboard V2: shared HOD/LOD event buffer. Created here so the API (api_v2)
    # and the post-bar hook below see the same instance.
    event_buffer = EventBuffer()
    app_state.event_buffer = event_buffer
    scanner.warmup(spy_daily, symbol_daily, sector_daily=sector_daily,
                   bars_5m=bars_5m,
                   session_date=pd.Timestamp.now(tz="America/New_York").date())

    # Load custom trigger definitions before replaying today's bars. The replay
    # can then prime edge/once memory without emitting alerts, so a mid-session
    # start does not announce an opening event as if it just happened.
    custom_eval = CustomEvaluator()
    scanner.attach_custom(custom_eval)
    custom_eval.warmup(symbol_daily, bars_5m)

    ready_count   = len(scanner._states)
    sector_mapped = sum(1 for s in scanner._states if s in sector_map)
    rvol_ready    = sum(1 for s in scanner._states if not scanner._states[s].volume_profile.empty)

    print(f"       {ready_count} symbols loaded")
    print(f"       {sector_mapped} with sector map   {rvol_ready} with RVOL profile")

    if sector_mapped == 0:
        print("\n  WARNING: sector_rrs gate has no data --no alerts will fire.")
    if rvol_ready == 0:
        print("\n  WARNING: rvol gate has no data --no alerts will fire.")

    # ── 5b. Seed today's SPY VWAP ─────────────────────────────────────────────
    # The session VWAP must be computed from the full session (9:30 onwards), not just
    # from the first live bar received. Without seeding, the scanner would see
    # VWAP ≈ current price for the first hour after startup → regime=NEUTRAL → no alerts.
    # today_spy is also reused in step 5c to seed each symbol's _spy_5m / rrs_m5.
    today_spy: pd.DataFrame = pd.DataFrame()
    print("       Seeding today's SPY VWAP ...", flush=True)
    try:
        today_spy = feed.get_todays_bars("SPY", "1Min")
        if not today_spy.empty and scanner._spy_state is not None:
            seeded = 0
            for ts, row in today_spy.iterrows():
                bar_dict = {
                    "symbol":    "SPY",
                    "timestamp": ts,
                    "open":      float(row["open"]),
                    "high":      float(row["high"]),
                    "low":       float(row["low"]),
                    "close":     float(row["close"]),
                    "volume":    float(row["volume"]),
                }
                scanner._spy_state.on_bar(bar_dict)
                scanner._latest_spy_bar = bar_dict
                scanner._regime = classify_market(bar_dict["close"], scanner._spy_state.vwap)
                seeded += 1
            # VWAP accumulates during regular hours only, so pre-market it is
            # legitimately None. Formatting it with :.2f raised inside the try
            # and printed "Could not seed SPY VWAP", which was alarming and
            # wrong: the bars above were seeded and the regime was set.
            v = scanner._spy_state.vwap
            print(
                f"       {seeded} SPY bars seeded  "
                f"vwap={f'{v:.2f}' if v is not None else 'none yet (pre-market)'}  "
                f"regime={scanner._regime.value.upper()}",
                flush=True,
            )
        else:
            print("       No SPY bars available for today yet (pre-market?)", flush=True)
    except Exception as exc:
        print(f"       WARNING: SPY VWAP seeding failed: {exc}", flush=True)
        log.warning("SPY VWAP seeding failed", exc_info=True)

    # ── 5c. Seed today's intraday state (VWAP, high/low, cum_vol, 5-min bars) ──
    # Without seeding, each stock's VWAP starts at $0 from first live bar
    # received — on a mid-day restart the intraday VWAP is badly wrong (e.g.
    # $80.76 when the session VWAP is $89.48 for a stock that opened high and
    # sold off).  Batch-fetch today's 1-min bars and replay them into each
    # state so VWAP, high/low, and 5-min bars are all correct at startup.
    #
    # SPY bars are also passed to each on_bar call so that each symbol's
    # _spy_5m deque is populated and rrs_m5 is computed from the replay.
    # Without this, rrs_m5 stays None for all symbols after a mid-session
    # restart — and once bars_5m_count >= 12, gate_rrs_d1 returns
    # passed=False (rrs_unavailable), blocking all alerts permanently.
    seed_started = time.perf_counter()
    print("       Seeding today's intraday state (VWAP + levels + 5-min bars) ...", flush=True)
    spy_bar_map: dict = {}
    for spy_ts, spy_row in today_spy.iterrows():
        spy_bar_map[spy_ts] = {
            "symbol":    "SPY",
            "timestamp": spy_ts,
            "open":      float(spy_row["open"]),
            "high":      float(spy_row["high"]),
            "low":       float(spy_row["low"]),
            "close":     float(spy_row["close"]),
            "volume":    float(spy_row["volume"]),
        }
    seeded_from_bars: set[str] = set()
    try:
        all_syms = scanner.ranked_symbols()      # most liquid first: a provider may seed only the top
        sector_syms = sorted(set(sector_map.values()) - {"SPY"})
        requested = all_syms + [s for s in sector_syms if s not in all_syms]
        today_bars = feed.get_todays_bars_multi(requested, "1Min")
        sector_bar_maps: dict[str, dict] = {}
        for sector_sym in sector_syms:
            rows = today_bars.get(sector_sym, pd.DataFrame())
            sector_bar_maps[sector_sym] = {
                ts: {
                    "symbol": sector_sym, "timestamp": ts,
                    "open": float(row["open"]), "high": float(row["high"]),
                    "low": float(row["low"]), "close": float(row["close"]),
                    "volume": float(row["volume"]),
                }
                for ts, row in rows.iterrows()
            }
        for sym in all_syms:
            sym_bars = today_bars.get(sym, pd.DataFrame())
            state = scanner._states.get(sym)
            if state is None or sym_bars.empty:
                continue
            state._reset_intraday()  # clear any partial state from warmup
            sector_bars = sector_bar_maps.get(sector_map.get(sym, ""), {})
            for ts, row in sym_bars.iterrows():
                scanner.seed_session_bar({
                    "symbol":    sym,
                    "timestamp": ts,
                    "open":      float(row["open"]),
                    "high":      float(row["high"]),
                    "low":       float(row["low"]),
                    "close":     float(row["close"]),
                    "volume":    float(row["volume"]),
                }, spy_bar_map.get(ts), sector_bars.get(ts))
            seeded_from_bars.add(sym)
        print(
            f"       {len(seeded_from_bars)}/{len(all_syms)} symbols seeded from bars"
            f"  ({len(all_syms) - len(seeded_from_bars)} missing)"
            f" in {time.perf_counter() - seed_started:.1f}s",
            flush=True,
        )
    except Exception as exc:
        print(f"       WARNING: Could not seed intraday bars after "
              f"{time.perf_counter() - seed_started:.1f}s: {exc}", flush=True)

    # A provider that can back-fill today's bars only for its most liquid symbols
    # (Schwab) fills the rest from quotes when the session is already under way:
    # one catch-up bar holding the day's open, high, low, last and volume so far.
    # It goes straight into the symbol's state, like the bars above, so it never
    # reaches a setup and cannot fire an alert.
    try:
        _now_et = pd.Timestamp.now(tz="America/New_York")
        _rth = _now_et.weekday() < 5 and (9 * 60 + 31) <= (_now_et.hour * 60 + _now_et.minute) < 16 * 60
        if _rth and hasattr(feed, "get_session_quotes"):
            todo = [s for s in scanner.ranked_symbols() if s not in seeded_from_bars]
            quotes = feed.get_session_quotes(todo) if todo else {}
            stamp = (_now_et.floor("min") - pd.Timedelta(minutes=1)).tz_convert("UTC")
            for sym, q in quotes.items():
                state = scanner._states.get(sym)
                if state is None:
                    continue
                state._reset_intraday()
                _bar = {"symbol": sym, "timestamp": stamp, "open": q["open"], "high": q["high"],
                        "low": q["low"], "close": q["last"], "volume": q["volume"]}
                state.on_bar(_bar)
                # The candle rings get the day's high and low only, never this bar:
                # it holds the whole session's volume, and as a candle it read as a
                # 10x to 25x volume spike the moment its 5-minute candle closed
                # (measured: 44 false Volume Spike alerts after one mid-session start).
                _ser = scanner.series(sym)
                _ser.session_date = _now_et.strftime("%Y-%m-%d")
                _ser.day_high, _ser.day_low = q["high"], q["low"]
                _ser.ext_high, _ser.ext_low = q["high"], q["low"]
            if todo:
                print(f"       {len(quotes)}/{len(todo)} more symbols caught up from quotes (volume and "
                      f"high/low so far; VWAP approximate until the next start before the open)", flush=True)
    except Exception as exc:
        print(f"       WARNING: Could not catch up from quotes: {exc}", flush=True)

    # (The old snapshot daily_volume fallback for symbols with no bars today was
    # removed: that figure includes premarket volume, which the RTH-only volume
    # profile does not, so it inflated RVOL. A symbol with no bars today has
    # nothing to alert on anyway.)

    # ── 5d. Start dashboard API ───────────────────────────────────────────────
    import threading
    import uvicorn
    if not args.no_fundamentals:
        fundamentals_cache = get_fundamentals_cache()
        provider_fetch = getattr(feed, "get_fundamentals", None)
        fundamentals_cache.configure_provider(
            provider_fetch if callable(provider_fetch) else None,
            args.feed if callable(provider_fetch) else None,
        )
        fundamentals_cache.start_background_prefetch(list(scanner._states.keys()))
        source = "Schwab Instruments + Yahoo profile" if callable(provider_fetch) else "Yahoo Finance"
        print(f"       Dashboard V2: {source} fundamentals prefetch running in background "
              "(--no-fundamentals to skip)", flush=True)
    _api_app = create_app(app_state)
    print(f"       Dashboard V2: http://localhost:{args.port}/v2  (build: npm --prefix dashboard-v2 run build)", flush=True)
    _server_cfg = uvicorn.Config(_api_app, host=args.host, port=args.port, log_level="warning")
    _api_server = uvicorn.Server(_server_cfg)
    _api_socks = bind_sockets(args.host, args.port)
    _api_thread = threading.Thread(target=_api_server.run, kwargs={"sockets": _api_socks},
                                   daemon=True, name="api-server")
    _api_thread.start()
    print(f"       Dashboard: http://localhost:{args.port}", flush=True)
    print(f"       Unified feed: ws://localhost:{args.port}/ws/alerts  (filters: sources, setups, triggers, "
          f"symbols, direction, min_score; archive {args.alerts_dir}/all)", flush=True)

    # ── 5e. Optional plugin setups, then custom setups ────────────────────────
    # A plugin may attach its own evaluators on the same stream and hand back
    # the sink custom setups should share. Otherwise custom setups get their
    # own sink, published on the unified feed as source "custom".
    import types
    custom_sink = None
    if ext is not None:
        custom_sink = ext.attach(types.SimpleNamespace(
            args=args, scanner=scanner, app_state=app_state, feed=feed, spy_daily=spy_daily))
    if custom_sink is None:
        custom_sink = app_state.hub.tap(_CustomPrintSink(), "custom")
    # Custom setups (dashboard Config > Setups): universe + composed triggers,
    # setup=<custom id>, custom=true. The evaluator was attached before today's
    # bars were replayed; replace its temporary sink with the unified feed sink.
    scanner.attach_custom(custom_eval, custom_sink)
    app_state.custom_eval = custom_eval

    # Universe profiles: the screen each setup is checked against before an
    # alert is emitted. Everything defaults to the empty "up_all" profile, so
    # this is inert until profiles are assigned in the dashboard.
    profile_engine = ProfileEngine()
    scanner.attach_profiles(profile_engine)
    profile_engine.resolve_members(scanner._states, get_fundamentals_cache())
    _assigned = {k: v for k, v in profile_engine.assignments.load().items() if v != "up_all"}
    _assigned.update({s["id"]: s["universe_profile"] for s in custom_eval.plan.setups
                      if s.get("universe_profile")})
    from collections import Counter
    _per = Counter(_assigned.values())
    print(f"       Universe profiles: {len(profile_engine.compiled)} loaded; "
          + (", ".join(f"{pid} used by {n}" for pid, n in _per.most_common())
             if _assigned else "none assigned (inert)"))

    # ── 6. Connect ────────────────────────────────────────────────────────────
    _step(6, TOTAL_STEPS, "Connecting to live feed ...")
    app_state.hub.set_status("connecting")

    def _on_stop(sig, frame):
        print("\n\nStopping ...", flush=True)
        # Raising KeyboardInterrupt lets asyncio.run() exit cleanly.
        # Calling stream.stop() from here deadlocks on Windows because the signal
        # is delivered inside the event loop's own I/O poll (IOCP wait), so the
        # loop can't process the stop coroutine and times out after 5 s.
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT,  _on_stop)
    signal.signal(signal.SIGTERM, _on_stop)

    _banner(
        f"LIVE  -- {ready_count} symbols   "
        f"sector={sector_mapped}   rvol={rvol_ready}\n"
        f"  Alerts will print here when they fire.\n"
        f"  Press Ctrl+C to stop."
    )

    # Wrap _on_bar to emit a heartbeat every 500 bars so we can confirm data is flowing.
    _bar_count = [0]
    _orig_on_bar = scanner._on_bar
    _hodlod_hook = make_hodlod_hook(scanner, event_buffer)   # Dashboard V2; never raises

    def _on_bar_diag(bar: dict) -> None:
        app_state.hub.record_market_bar(bar.get("timestamp"))
        _bar_count[0] += 1
        n = _bar_count[0]
        if n in (1, 50, 200) or n % 500 == 0:
            regime = scanner._regime.value.upper()
            print(f"  [heartbeat] bars={n:,}  regime={regime}  alerts={app_state.hub.published}", flush=True)
        _orig_on_bar(bar)
        _hodlod_hook(bar)

    scanner._on_bar = _on_bar_diag

    try:
        scanner.connect()
    except (KeyboardInterrupt, TimeoutError):
        pass
    finally:
        app_state.hub.set_status("stopping")

    # ── Session summary (after Ctrl-C) ────────────────────────────────────────
    # Capture alerts BEFORE reset_session: reset clears the sinks.
    alerts = list(app_state.hub.recent)
    scanner.reset_session()
    _banner(f"Session ended  -- {len(alerts)} alert(s) fired today")
    if alerts:
        print(f"  {'TIME':>7}  {'SYMBOL':<6}  {'DIR':<5}  {'SETUP'}")
        print(f"  {'-'*7}  {'-'*6}  {'-'*5}  {'-'*22}")
        for a in sorted(alerts, key=lambda x: str(x.get("timestamp", ""))):
            try:
                ts = pd.Timestamp(a["timestamp"]).tz_convert("America/New_York").strftime("%H:%M")
            except Exception:
                ts = "?"
            print(f"  {ts:>7}  {a['symbol']:<6}  {a['direction']:<5}  "
                  f"{a.get('setup_label') or a.get('setup')}")
    print()


if __name__ == "__main__":
    main()
