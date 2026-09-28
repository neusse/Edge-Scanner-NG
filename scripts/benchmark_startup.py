"""Repeatable, network-free benchmark for current-session state seeding."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from scanner.alert_sink import AlertSink
from scanner.data.interface import DataFeed
from scanner.live_scanner import LiveScanner


class _NoNetworkFeed(DataFeed):
    def subscribe_minute_bars(self, symbols, callback):
        raise NotImplementedError

    def get_historical_daily(self, symbol, start, end):
        raise NotImplementedError

    def get_historical_bars(self, symbol, timeframe, start, end):
        raise NotImplementedError

    def get_snapshot(self, symbols):
        raise NotImplementedError


def _daily(base: float) -> pd.DataFrame:
    index = pd.date_range("2025-01-02", periods=220, freq="B", tz="UTC")
    close = pd.Series([base + i * 0.02 for i in range(len(index))], index=index)
    return pd.DataFrame({
        "open": close - 0.05, "high": close + 0.30, "low": close - 0.30,
        "close": close, "volume": 1_000_000.0,
    }, index=index)


def _bars(symbol: str, count: int) -> list[dict]:
    index = pd.date_range("2026-09-25 09:30", periods=count, freq="min",
                          tz="America/New_York")
    return [{
        "symbol": symbol, "timestamp": stamp,
        "open": 100.0 + i * 0.01, "high": 100.08 + i * 0.01,
        "low": 99.95 + i * 0.01, "close": 100.03 + i * 0.01,
        "volume": 1_000.0 + i, "source": "benchmark",
    } for i, stamp in enumerate(index)]


def benchmark(symbol_count: int = 256, bar_count: int = 300) -> float:
    symbols = [f"S{i:04d}" for i in range(symbol_count)]
    scanner = LiveScanner(symbols, _NoNetworkFeed(), AlertSink())
    scanner.warmup(_daily(450.0), {symbol: _daily(100.0) for symbol in symbols})
    for symbol in symbols:
        for period in (3, 8, 9, 21):
            scanner.series(symbol).want_ema(5, period)
    started = time.perf_counter()
    for symbol in symbols:
        scanner.seed_session_bars(_bars(symbol, bar_count))
    return time.perf_counter() - started


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=256)
    parser.add_argument("--bars", type=int, default=300)
    parser.add_argument("--budget-seconds", type=float, default=30.0)
    args = parser.parse_args()
    elapsed = benchmark(args.symbols, args.bars)
    print(f"startup seed benchmark: {args.symbols} symbols x {args.bars} bars = {elapsed:.3f}s")
    if elapsed > args.budget_seconds:
        raise SystemExit(
            f"benchmark exceeded {args.budget_seconds:.1f}s budget by "
            f"{elapsed - args.budget_seconds:.1f}s"
        )


if __name__ == "__main__":
    main()
