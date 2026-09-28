"""Bounded, read-only Alpaca stock-data stream probe; never starts Edge or trades.

Example: .venv/Scripts/python.exe scripts/probe_alpaca_stream.py SPY AAPL WDC
Requires alpaca_paper_api_key and alpaca_paper_secret in the environment.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

from alpaca.data.enums import DataFeed
from alpaca.data.live import StockDataStream

_SERVICES = {
    "quotes": ("quotes", "subscribe_quotes"),
    "trades": ("trades", "subscribe_trades"),
    "bars": ("bars", "subscribe_bars"),
    "updated_bars": ("updatedBars", "subscribe_updated_bars"),
    "daily_bars": ("dailyBars", "subscribe_daily_bars"),
    "statuses": ("statuses", "subscribe_trading_statuses"),
}
_SYMBOL = re.compile(r"[A-Z][A-Z0-9.\-]{0,9}\Z")
_PRICE_FIELDS = {
    "quotes": {"bid": "bp", "ask": "ap", "bid_size": "bs", "ask_size": "as"},
    "trades": {"price": "p", "size": "s"},
    "bars": {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v",
             "trade_count": "n", "vwap": "vw"},
    "updatedBars": {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v",
                    "trade_count": "n", "vwap": "vw"},
    "dailyBars": {"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v",
                  "trade_count": "n", "vwap": "vw"},
}


def market_timestamp(raw: object) -> datetime | None:
    """Alpaca's raw WebSocket decoder may return a protobuf Timestamp object."""
    try:
        if hasattr(raw, "seconds") and hasattr(raw, "nanoseconds"):
            return datetime.fromtimestamp(raw.seconds + raw.nanoseconds / 1e9, timezone.utc)
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return stamp if stamp.tzinfo else None
    except (TypeError, ValueError, OverflowError):
        return None


def event_lag_ms(event: dict, channel: str, received: datetime) -> float | None:
    """Market-event lag; minute bars are measured from their *close*, not start."""
    stamp = market_timestamp(event.get("t"))
    if stamp is None:
        return None
    if channel in ("bars", "updatedBars"):
        stamp += timedelta(minutes=1)
    return round((received - stamp).total_seconds() * 1000, 1)


class ProbeRecorder:
    def __init__(self, requested: dict[str, list[str]]) -> None:
        self.requested = requested
        self.accepted: dict[str, list[str]] | None = None
        self.controls: list[dict] = []
        self.errors: list[dict] = []
        self.counts: Counter[tuple[str, str]] = Counter()
        self.lags: dict[str, list[float]] = {}
        self.samples: list[dict] = []
        self.latest: dict[str, dict[str, dict]] = {}

    def control(self, message: dict) -> None:
        kind = message.get("T")
        if kind == "subscription":
            self.accepted = {name: list(message.get(name) or []) for name in self.requested}
        elif kind == "error":
            self.errors.append({"code": message.get("code"), "message": message.get("msg")})
        elif kind == "success":
            self.controls.append({"type": kind, "message": message.get("msg")})

    def event(self, message: dict, channel: str, received: datetime | None = None) -> None:
        received = received or datetime.now(timezone.utc)
        symbol = str(message.get("S") or "")
        self.counts[(channel, symbol)] += 1
        lag = event_lag_ms(message, channel, received)
        if lag is not None:
            self.lags.setdefault(channel, []).append(lag)
        stamp = market_timestamp(message.get("t"))
        sample = {"symbol": symbol,
                  "market_timestamp": stamp.isoformat() if stamp else None,
                  "received_at": received.isoformat(), "lag_ms": lag}
        sample.update({name: message[key] for name, key in _PRICE_FIELDS.get(channel, {}).items()
                       if key in message})
        self.latest.setdefault(channel, {})[symbol] = sample
        if self.counts[(channel, symbol)] <= 2:
            self.samples.append({"channel": channel, **sample})

    def report(self, *, feed: str, symbols: list[str], duration: int) -> dict:
        return {
            "feed": feed, "credential_mode": "paper", "duration_seconds": duration,
            "requested_symbols": symbols, "requested": self.requested,
            "accepted": self.accepted, "controls": self.controls, "errors": self.errors,
            "counts": {channel: {symbol: self.counts[(channel, symbol)] for symbol in symbols}
                       for channel in self.requested},
            "lag_ms": {channel: {"min": min(values), "max": max(values),
                                  "mean": round(sum(values) / len(values), 1)}
                       for channel, values in self.lags.items() if values},
            "latest": self.latest,
            "samples": self.samples,
            "note": "No messages does not prove rejection; use accepted subscriptions. "
                    "Bar lag is measured from the minute close. IEX is one exchange, not SIP.",
        }


class ObservedStockStream(StockDataStream):
    def __init__(self, *args, recorder: ProbeRecorder, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.recorder = recorder

    async def _dispatch(self, message: dict) -> None:
        self.recorder.control(message)
        await super()._dispatch(message)

    async def _auth(self) -> None:
        try:
            await super()._auth()
        except Exception as exc:
            self.recorder.errors.append({"code": "authentication_or_entitlement",
                                         "message": str(exc)})
            raise


async def probe(feed: str, symbols: list[str], services: list[str], duration: int,
                api_key: str, secret_key: str) -> dict:
    requested = {channel: symbols for service in services for channel, _ in (_SERVICES[service],)}
    recorder = ProbeRecorder(requested)
    stream = ObservedStockStream(api_key, secret_key, feed=DataFeed(feed), raw_data=True,
                                 recorder=recorder)
    for service in services:
        channel, method = _SERVICES[service]

        async def record(message: dict, observed_channel: str = channel) -> None:
            recorder.event(message, observed_channel)

        getattr(stream, method)(record, *symbols)

    async def stop_after() -> None:
        await asyncio.sleep(duration)
        await stream.stop_ws()

    stopper = asyncio.create_task(stop_after())
    try:
        await asyncio.wait_for(stream._run_forever(), timeout=duration + 12)
    except asyncio.TimeoutError:
        recorder.errors.append({"code": "timeout", "message": "stream did not stop in time"})
        await stream.stop_ws()
    finally:
        stopper.cancel()
        await stream.close()
    return recorder.report(feed=feed, symbols=symbols, duration=duration)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("symbols", nargs="*", default=["SPY", "AAPL", "WDC"])
    parser.add_argument("--feed", choices=("iex", "sip"), default="iex")
    parser.add_argument("--services", default="quotes,trades,bars",
                        help="Comma-separated: quotes,trades,bars,updated_bars,daily_bars,statuses")
    parser.add_argument("--duration", type=int, default=25, help="5 to 120 seconds")
    args = parser.parse_args(argv)
    symbols = list(dict.fromkeys(str(s).upper().strip() for s in args.symbols))
    services = list(dict.fromkeys(s.strip() for s in args.services.split(",")))
    if (not symbols or len(symbols) > 30 or any(not _SYMBOL.fullmatch(s) for s in symbols)
            or not services or any(s not in _SERVICES for s in services)
            or not 5 <= args.duration <= 120):
        parser.error("Use 1-30 valid symbols, supported services, and a duration of 5-120 seconds")
    api_key = os.environ.get("alpaca_paper_api_key")
    secret_key = os.environ.get("alpaca_paper_secret")
    if not api_key or not secret_key:
        print("Missing alpaca_paper_api_key or alpaca_paper_secret; no stream opened.", file=sys.stderr)
        return 2
    try:
        result = asyncio.run(probe(args.feed, symbols, services, args.duration, api_key, secret_key))
    except Exception as exc:
        # The SDK can reject a feed before the first callback. Never print key material.
        detail = str(exc).replace(api_key, "[redacted]").replace(secret_key, "[redacted]")
        print(json.dumps({"feed": args.feed, "credential_mode": "paper",
                          "error": f"{type(exc).__name__}: {detail}"}), file=sys.stderr)
        return 1
    report_json = json.dumps(result, indent=2, default=str)
    print(report_json.replace(api_key, "[redacted]").replace(secret_key, "[redacted]"))
    return 1 if result["errors"] or result["accepted"] is None else 0


if __name__ == "__main__":
    raise SystemExit(main())
