"""1-minute bars built from quotes, for providers that cap their bar stream.

Schwab streams real 1-minute bars for at most 300 symbols per account, but
quotes for far more (3,000 on its level-one stream, 500 per REST request). A
quote carries the last trade price and the cumulative volume for the day, which
is enough to rebuild a minute bar:

    open / close   first and last trade price seen in the minute
    high / low     highest and lowest trade price SEEN in the minute
    volume         growth of the day's cumulative volume across the minute

How close that is to a real bar depends on how often quotes arrive. On a live
quote stream (several updates a second) the only thing lost is a spike that
appears and reverts between two updates. When quotes are polled every few
seconds, open, close and volume stay good and high / low are approximate. One
correction makes the case that matters most exact again: when the quote's own
high or low OF THE DAY moves, that extreme is known precisely, so a new high or
low of day is never missed however slowly the quotes arrive.

Bars follow the same rules as real ones: stamped at the start of their minute in
UTC, emitted once after the minute ends, and only for minutes that traded. A
minute with no trade produces no bar.

ODD LOTS. On the consolidated tape an odd-lot trade counts toward volume but may
not set a bar's open, high, low or close, and real bars follow that rule. A
quote's "last price" does not: it shows the most recent trade of any size, and
in thin trading (premarket above all) most prints are odd lots at off-market
prices. Measured on the first live morning, bars built from every last price had
premarket highs and lows about 0.7% wider than real bars, up to 8%. So a price
only counts when the quote's last size is a round lot for that price; an
odd-lot print still adds its volume. A minute with volume but no round-lot
price produces no bar, as on the tape.

Thread-safe: quotes may arrive from a stream thread and a polling thread while
a timer thread flushes finished minutes.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

import pandas as pd

_RESET_FRACTION = 0.2


def round_lot(price: float) -> int:
    """Shares in a round lot at this price (SEC tiers in force since late 2025)."""
    if price <= 250.0:
        return 100
    if price <= 1000.0:
        return 40
    if price <= 10000.0:
        return 10
    return 1


@dataclass
class _Sym:
    last: Optional[float] = None          # last ROUND-LOT trade price seen
    raw_last: Optional[float] = None      # last trade price of any size
    last_size: Optional[float] = None     # size of the most recent trade, in shares
    priced: bool = False                  # a round-lot price arrived in the bar being built
    pending: bool = False                 # a round-lot price arrived that no bar has used yet
    total: Optional[float] = None         # cumulative day volume seen
    day_high: Optional[float] = None
    day_low: Optional[float] = None
    minute: Optional[int] = None          # epoch minute of the bar being built
    o: float = 0.0
    h: float = 0.0
    l: float = 0.0
    c: float = 0.0
    v: float = 0.0
    grace: float = 3.0                    # seconds after the minute ends before it may be emitted


class QuoteBarBuilder:
    """Turns quote updates into 1-minute bars and hands them to `emit`.

    Args:
        emit:  called with one bar dict (symbol, timestamp, open, high, low,
               close, volume, source) per symbol per traded minute.
        clock: wall clock in epoch seconds; injectable for tests.
    """

    def __init__(self, emit: Callable[[dict], None], clock: Callable[[], float] = time.time) -> None:
        self._emit = emit
        self._clock = clock
        self._syms: dict[str, _Sym] = {}
        self._lock = threading.Lock()
        self.bars_emitted = 0
        self.quotes_seen = 0

    def track(self, symbol: str, grace: float) -> None:
        """Register a symbol. `grace` is how long after a minute ends its bar is
        held back: a little over the longest gap between two quotes for it."""
        with self._lock:
            self._syms.setdefault(symbol, _Sym()).grace = grace

    def on_quote(self, symbol: str, last: Optional[float] = None, total_volume: Optional[float] = None,
                 day_high: Optional[float] = None, day_low: Optional[float] = None,
                 last_size: Optional[float] = None) -> None:
        """One quote update. Any field may be missing (a stream sends only what
        changed); the last known value is kept. `last_size` is in shares; when a
        provider never sends it, every price counts."""
        done: Optional[dict] = None
        with self._lock:
            s = self._syms.get(symbol)
            if s is None:
                return
            self.quotes_seen += 1
            if last_size is not None:
                s.last_size = float(last_size)
            if last is not None and last > 0:
                s.raw_last = float(last)
            # The price counts only when the trade that set it was a round lot.
            eligible = s.raw_last is not None and (s.last_size is None or s.last_size >= round_lot(s.raw_last))
            # A stream may send the price and the volume of one trade in separate
            # updates, so a new round-lot price waits for the next volume change.
            if eligible and (last is not None or last_size is not None):
                s.pending = True
                s.last = s.raw_last
            new_high = day_high is not None and s.day_high is not None and day_high > s.day_high
            new_low = day_low is not None and s.day_low is not None and 0 < day_low < s.day_low
            if day_high is not None and day_high > 0:
                s.day_high = float(day_high)
            if day_low is not None and day_low > 0:
                s.day_low = float(day_low)

            if total_volume is None:
                return
            total = float(total_volume)
            if s.total is None:                       # first sight: a baseline, not a trade
                s.total = total
                s.pending = False
                return
            if total == s.total:
                return                                # bid/ask moved, nothing traded
            if total > s.total:
                delta = total - s.total
            elif total < s.total * _RESET_FRACTION:
                # A genuine counter reset starts a new cumulative baseline.
                delta = total
            else:
                # A stale/corrected snapshot must not lower the high-water mark:
                # counting the climb back would count the same trades twice.
                return
            s.total = total
            if delta <= 0:
                return

            minute = int(self._clock() // 60)
            if s.minute is not None and minute != s.minute:
                done = self._close(symbol, s)
            if s.minute is None:
                s.minute, s.v, s.priced = minute, 0.0, False
            s.v += delta                              # every print counts toward volume
            if s.pending or (eligible and not s.priced):
                s.pending = False
                if not s.priced:
                    s.o = s.h = s.l = s.last
                    s.priced = True
                s.c = s.last
                s.h = max(s.h, s.last)
                s.l = min(s.l, s.last)
            # The day's extreme moved since the previous quote, so it was printed
            # inside this bar: take it exactly, even if no quote caught it.
            if s.priced and new_high and s.day_high is not None and s.day_high >= s.h:
                s.h = s.day_high
            if s.priced and new_low and s.day_low is not None and s.day_low <= s.l:
                s.l = s.day_low
        if done is not None:
            self._send(done)

    def flush(self) -> int:
        """Emit every bar whose minute has ended and whose grace has passed.
        Call about once a second. Returns how many bars were emitted."""
        now = self._clock()
        out: list[dict] = []
        with self._lock:
            for symbol, s in self._syms.items():
                if s.minute is not None and now >= (s.minute + 1) * 60 + s.grace:
                    bar = self._close(symbol, s)
                    if bar is not None:
                        out.append(bar)
        for bar in out:
            self._send(bar)
        return len(out)

    def _close(self, symbol: str, s: _Sym) -> Optional[dict]:
        if not s.priced:                  # volume but no round-lot price: no bar, as on the tape
            s.minute = None
            return None
        bar = {
            "symbol": symbol,
            "timestamp": pd.Timestamp(s.minute * 60, unit="s", tz="UTC"),
            "open": s.o, "high": s.h, "low": s.l, "close": s.c, "volume": s.v,
            "source": "quotes",
        }
        s.minute = None
        return bar

    def _send(self, bar: dict) -> None:
        self.bars_emitted += 1
        self._emit(bar)
