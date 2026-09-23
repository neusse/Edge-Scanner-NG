"""Conservative, bounded opening-range trade-cross detection.

Only completed, authoritative 1-minute bars may establish the range. A Level
One update is an observed trade only when raw price and trade-time fields occur
together; cached quote-book values are never used to manufacture a crossing.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from zoneinfo import ZoneInfo

import pandas as pd

_ET = ZoneInfo("America/New_York")
_OPEN = 9 * 60 + 30
_CLOSE = 16 * 60
_BAR_SOURCES = {"schwab_chart_equity", "schwab_history_1m"}
_MAX_TRADE_AGE_MS = 2_000
_MAX_CONTINUITY_GAP_MS = 10_000


class OpeningRangeBook:
    """At most 15 opening bars per symbol for the current Eastern date."""

    def __init__(self) -> None:
        self._bars: dict[str, tuple[str, dict[int, dict]]] = {}
        self._cache: dict[tuple[str, str, int], dict] = {}

    def clear(self) -> None:
        self._bars.clear()
        self._cache.clear()

    def on_bar(self, bar: dict) -> None:
        try:
            stamp = pd.Timestamp(bar["timestamp"])
            if stamp.tzinfo is None:
                return
            et = stamp.tz_convert(_ET)
            minute = et.hour * 60 + et.minute
            symbol = str(bar["symbol"]).upper()
        except (KeyError, TypeError, ValueError):
            return
        if not _OPEN <= minute < _OPEN + 15:
            return
        day = et.strftime("%Y-%m-%d")
        previous = self._bars.get(symbol)
        if previous and previous[0] != day:
            self._cache.pop((symbol, previous[0], 5), None)
            self._cache.pop((symbol, previous[0], 15), None)
        bars = previous[1] if previous and previous[0] == day else {}
        bars[minute] = {"high": float(bar["high"]), "low": float(bar["low"]),
                        "source": bar.get("source"), "timestamp": stamp.tz_convert("UTC").isoformat()}
        self._bars[symbol] = (day, bars)
        self._cache.pop((symbol, day, 5), None)
        self._cache.pop((symbol, day, 15), None)

    def completed(self, symbol: str, day: str, interval: int) -> dict | None:
        if interval not in (5, 15):
            return None
        key = (symbol.upper(), day, interval)
        if key in self._cache:
            return self._cache[key]
        row = self._bars.get(symbol.upper())
        if row is None or row[0] != day:
            return None
        opening = [row[1].get(_OPEN + offset) for offset in range(interval)]
        if any(bar is None or bar["source"] not in _BAR_SOURCES for bar in opening):
            return None
        digest = hashlib.sha256(json.dumps(opening, sort_keys=True).encode()).hexdigest()[:16]
        result = {"id": f"{symbol.upper()}:{day}:rth-open:{interval}m:{digest}",
                "interval_min": interval, "high": max(bar["high"] for bar in opening),
                "low": min(bar["low"] for bar in opening),
                "start": opening[0]["timestamp"],
                "end": (pd.Timestamp(opening[-1]["timestamp"]) + pd.Timedelta(minutes=1)).isoformat(),
                "bar_sources": sorted({bar["source"] for bar in opening}),
                "complete": True}
        self._cache[key] = result
        return result


class OrbTradeCross:
    """One observed below-to-above crossing per symbol/interval/Eastern day."""

    def __init__(self) -> None:
        self._state: dict[tuple[str, str, int], dict] = {}

    def clear(self) -> None:
        self._state.clear()

    def restore(self, alerts: list[dict]) -> None:
        for alert in alerts:
            evidence = alert.get("trade_cross") or {}
            if alert.get("eventSemantics") != "trade-cross" or not evidence:
                continue
            try:
                ts = pd.Timestamp(evidence["trade_market_timestamp"]).tz_convert(_ET)
                key = (str(alert["symbol"]).upper(), ts.strftime("%Y-%m-%d"),
                       int(evidence["opening_range"]["interval_min"]))
                self._state[key] = {"fired": True, "generation": None,
                                    "last_ms": None, "below": False, "reason": "fired_today"}
            except (KeyError, TypeError, ValueError):
                continue

    def observe(self, event: dict, opening: dict | None, interval: int) -> dict | None:
        """Return crossing evidence, or None when continuity is unavailable."""
        if opening is None or interval not in (5, 15):
            return None
        try:
            symbol = str(event["symbol"]).upper()
            trade_ms = int(event["trade_market_ms"])
            receipt_ms = int(event["receipt_ms"])
            price = float(event["price"])
            et = datetime.fromtimestamp(trade_ms / 1000, timezone.utc).astimezone(_ET)
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        minute = et.hour * 60 + et.minute
        day = et.strftime("%Y-%m-%d")
        key = (symbol, day, interval)
        if (not math.isfinite(price) or price <= 0
                or not _OPEN + interval <= minute < _CLOSE
                or not opening["id"].startswith(f"{symbol}:{day}:rth-open:{interval}m:")
                or event.get("source") != "schwab_levelone"
                or event.get("tier") != "stream"
                or event.get("coverage") != "live"
                or event.get("quality") != "valid"
                or event.get("delayed") is not False
                or event.get("stream_active") is not True
                or not 0 <= receipt_ms - trade_ms <= _MAX_TRADE_AGE_MS):
            prior = self._state.get(key)
            if prior is not None and not prior.get("fired"):
                prior.update(last_ms=None, below=False, reason="untrusted_update")
            return None
        generation = (event.get("stream_id"), event.get("connection_epoch"))
        state = self._state.get(key)
        if state is not None and state.get("fired"):
            return None
        if state is None or state["generation"] != generation:
            self._state[key] = {"generation": generation, "last_ms": trade_ms,
                                "below": price <= opening["high"], "fired": False,
                                "opening_id": opening["id"],
                                "reason": "already_above" if price > opening["high"] else "armed"}
            return None
        if state.get("opening_id") != opening["id"]:
            state.update(last_ms=trade_ms, below=price <= opening["high"],
                         opening_id=opening["id"], reason="opening_range_changed")
            return None
        if state["last_ms"] is None:
            state.update(last_ms=trade_ms, below=price <= opening["high"],
                         reason="armed" if price <= opening["high"] else "awaiting_below")
            return None
        if trade_ms == state["last_ms"]:
            return None
        if trade_ms < state["last_ms"]:
            state.update(last_ms=None, below=False, reason="out_of_order_trade")
            return None
        if trade_ms - state["last_ms"] > _MAX_CONTINUITY_GAP_MS:
            state.update(last_ms=trade_ms, below=price <= opening["high"], reason="trade_gap")
            return None
        crossed = state["below"] and price > opening["high"]
        state.update(last_ms=trade_ms, below=price <= opening["high"],
                     reason="armed" if price <= opening["high"] else "awaiting_below")
        if not crossed:
            return None
        state["fired"] = True
        return {"trade_price": price,
                "trade_market_timestamp": datetime.fromtimestamp(
                    trade_ms / 1000, timezone.utc).isoformat(),
                "receipt_timestamp": datetime.fromtimestamp(
                    receipt_ms / 1000, timezone.utc).isoformat(),
                "latency_ms": receipt_ms - trade_ms,
                "opening_range": dict(opening),
                "source": event["source"], "tier": event["tier"],
                "coverage": event["coverage"], "quote_quality": event.get("quality"),
                "stream_id": event.get("stream_id"),
                "connection_epoch": event.get("connection_epoch")}

    def status(self, symbol: str, day: str, interval: int,
               generation: tuple | None = None) -> dict:
        state = self._state.get((symbol.upper(), day, interval))
        if state is None:
            return {"state": "continuity_unavailable", "reason": "no_observed_baseline"}
        if state.get("fired"):
            return {"state": "fired_today", "reason": "fired_today"}
        if generation is not None and state.get("generation") != generation:
            return {"state": "continuity_unavailable", "reason": "stream_generation_changed"}
        if state.get("below"):
            return {"state": "armed", "reason": "observed_at_or_below_range_high"}
        return {"state": "continuity_unavailable", "reason": state.get("reason") or "awaiting_below"}
