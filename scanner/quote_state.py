"""Bounded, thread-safe Level One equity observations shared by API and setups.

Timestamps are market timestamps from Schwab, never inferred from receipt time.
Sparse updates retain older fields and therefore retain each field's own age.
"""
from __future__ import annotations

import math
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")


def _ms(value):
    try:
        value = int(value)
        return value if value > 1_000_000_000_000 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _price(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


class QuoteBook:
    """One current row per covered symbol; 1-second chart samples and update ring.

    Quote freshness is evaluated at read time. A consumer must choose its own
    trading eligibility threshold; the default 10 seconds is a display status.
    """

    def __init__(self, *, max_symbols=10000, history_per_symbol=1800, max_history_total=250000,
                 max_updates=10000, stale_after_ms=10000):
        self.max_symbols = max_symbols
        self.stream_id = str(uuid.uuid4())
        self.connection_epoch = 0
        self.history_per_symbol = history_per_symbol
        self.stale_after_ms = stale_after_ms
        self._lock = threading.RLock()
        self._rows = {}
        self._history = {}
        self._history_order = deque()
        self.max_history_total = max_history_total
        self._updates = deque(maxlen=max_updates)
        self._seq = 0

    def cover(self, symbol, source="schwab_levelone", tier="stream", status="subscribing"):
        symbol = str(symbol).upper().strip()
        if not symbol:
            return False
        with self._lock:
            if symbol not in self._rows and len(self._rows) >= self.max_symbols:
                return False
            row = self._rows.setdefault(symbol, {"symbol": symbol, "fields": {}, "delayed": None})
            row.update(source=source, tier=tier, coverage=status)
            return True

    def coverage(self, symbol, status):
        with self._lock:
            row = self._rows.get(symbol.upper())
            if row is not None:
                row["coverage"] = status

    def connection(self, active: bool):
        """Mark stream interruption without making retained prices appear fresh."""
        with self._lock:
            if not active:
                self.connection_epoch += 1
            for row in self._rows.values():
                if row["tier"] != "stream" or row["coverage"] in ("cap_exceeded", "not_watched"):
                    continue
                if not active:
                    row["coverage"] = "reconnecting"
                elif row["coverage"] == "reconnecting":
                    row["coverage"] = "subscribing"
                if not active:
                    row["delayed"] = None

    def ingest(self, symbol, fields, *, receipt_ms=None, source="schwab_levelone",
               tier="stream", delayed=None, block_ms=None):
        """Merge only present fields; reject older updates for each field.

        `fields` maps bid/ask/last to (price, market_ms, size). A price without
        a side/trade timestamp is kept but marked unverified, never given a
        fabricated market timestamp. A supplied non-positive price is invalid.
        """
        symbol = str(symbol).upper().strip()
        receipt_ms = int(receipt_ms if receipt_ms is not None else time.time() * 1000)
        with self._lock:
            if not self.cover(symbol, source, tier, "live"):
                return None
            row = self._rows[symbol]
            row["receipt_ms"] = receipt_ms
            if delayed is not None:
                row["delayed"] = bool(delayed)
            changed = False
            for side, incoming in fields.items():
                if side not in ("bid", "ask", "last") or incoming is None:
                    continue
                value, market_ms, size = incoming
                old = row["fields"].get(side)
                market_ms = _ms(market_ms) or (old or {}).get("market_ms")
                if old and market_ms and old.get("market_ms") and market_ms < old["market_ms"]:
                    continue
                if old and market_ms is None and old.get("market_ms") is not None:
                    continue
                price = _price(value) if value is not None else (old or {}).get("price")
                size_value = (old or {}).get("size")
                if size is not None:
                    try:
                        size_value = int(size)
                        if size_value < 0:
                            size_value = None
                    except (TypeError, ValueError):
                        pass
                item = {"price": price, "market_ms": market_ms,
                        "receipt_ms": receipt_ms, "size": size_value,
                        "valid": price is not None}
                if old != item:
                    row["fields"][side] = item
                    changed = True
            if not changed:
                return self._snapshot(row, receipt_ms)
            self._seq += 1
            snap = self._snapshot(row, receipt_ms)
            snap["seq"] = self._seq
            self._updates.append(snap)
            hist = self._history.setdefault(symbol, deque(maxlen=self.history_per_symbol))
            sample = {"time_ms": receipt_ms, "market_ms": max(
                (x["market_ms"] or 0 for x in row["fields"].values()), default=0) or None,
                "bid": snap["bid"], "ask": snap["ask"], "last": snap["last"],
                "spread": snap["spread"], "spread_bps": snap["spread_bps"],
                "quality": snap["quality"], "seq": self._seq}
            if hist and hist[-1]["time_ms"] // 1000 == receipt_ms // 1000:
                hist[-1] = sample
            else:
                hist.append(sample)
                self._history_order.append((symbol, receipt_ms // 1000))
                while len(self._history_order) > self.max_history_total:
                    old_symbol, old_second = self._history_order.popleft()
                    old_hist = self._history.get(old_symbol)
                    if old_hist and old_hist[0]["time_ms"] // 1000 == old_second:
                        old_hist.popleft()
            return snap

    def _snapshot(self, row, now_ms):
        fields = row["fields"]
        data = {side: fields.get(side, {}).get("price") for side in ("bid", "ask", "last")}
        bid, ask = data["bid"], data["ask"]
        side_age = {side: (max(0, now_ms - item["market_ms"]) if item.get("market_ms") else None)
                    for side, item in fields.items()}
        delayed = row.get("delayed")
        if row.get("coverage") in ("reconnecting", "cap_exceeded", "not_watched"):
            quality = "unavailable"
        elif any(s in fields and not fields[s]["valid"] for s in ("bid", "ask")):
            quality = "invalid"
        elif bid is None or ask is None:
            quality = "missing"
        elif ask < bid:
            quality = "crossed"
        elif ask == bid:
            quality = "locked"
        elif delayed is True:
            quality = "delayed"
        elif any(side_age.get(s) is None for s in ("bid", "ask")):
            quality = "unverified_time"
        elif any(side_age[s] > self.stale_after_ms for s in ("bid", "ask")):
            quality = "stale"
        elif row.get("coverage") != "live":
            quality = "unavailable"
        else:
            quality = "valid"
        midpoint = (bid + ask) / 2 if bid is not None and ask is not None else None
        spread = ask - bid if midpoint is not None and ask >= bid else None
        return {"symbol": row["symbol"], "stream_id": self.stream_id,
                "connection_epoch": self.connection_epoch, **data,
                "bid_size": fields.get("bid", {}).get("size"),
                "ask_size": fields.get("ask", {}).get("size"),
                "last_size": fields.get("last", {}).get("size"),
                "bid_market_ms": fields.get("bid", {}).get("market_ms"),
                "ask_market_ms": fields.get("ask", {}).get("market_ms"),
                "last_market_ms": fields.get("last", {}).get("market_ms"),
                "bid_age_ms": side_age.get("bid"), "ask_age_ms": side_age.get("ask"),
                "last_age_ms": side_age.get("last"), "receipt_ms": row.get("receipt_ms"),
                "delayed": delayed, "source": row["source"], "tier": row["tier"],
                "coverage": row["coverage"], "session": self._session(row, now_ms),
                "quality": quality, "midpoint": midpoint, "spread": spread,
                "spread_bps": 10000 * spread / midpoint if spread is not None else None}

    @staticmethod
    def _session(row, now_ms):
        stamp = max((v.get("market_ms") or 0 for v in row["fields"].values()), default=0) or now_ms
        dt = datetime.fromtimestamp(stamp / 1000, timezone.utc).astimezone(_ET)
        minute = dt.hour * 60 + dt.minute
        return "premarket" if 240 <= minute < 570 else "rth" if minute < 960 and minute >= 570 else "afterhours"

    def get(self, symbol, *, now_ms=None):
        with self._lock:
            row = self._rows.get(symbol.upper())
            return self._snapshot(row, int(now_ms if now_ms is not None else time.time() * 1000)) if row else None

    def history(self, symbol, limit=600):
        with self._lock:
            return list(self._history.get(symbol.upper(), ()))[-min(max(int(limit), 1), 1800):]

    def updates(self, since=0, limit=200):
        with self._lock:
            since, limit = max(0, int(since)), min(max(1, int(limit)), 1000)
            oldest = self._updates[0]["seq"] if self._updates else self._seq + 1
            now_ms = int(time.time() * 1000)
            rows = [{**u, "bid_age_ms": max(0, now_ms - u["bid_market_ms"]) if u["bid_market_ms"] else None,
                     "ask_age_ms": max(0, now_ms - u["ask_market_ms"]) if u["ask_market_ms"] else None,
                     "last_age_ms": max(0, now_ms - u["last_market_ms"]) if u["last_market_ms"] else None}
                    for u in self._updates if u["seq"] > since][:limit]
            return {"stream_id": self.stream_id, "seq": self._seq, "oldest_seq": oldest,
                    "gap": since < oldest - 1,
                    "updates": rows}

    def symbols(self):
        with self._lock:
            return list(self._rows)
