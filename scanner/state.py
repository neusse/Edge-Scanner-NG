"""SymbolState — all per-symbol state consumed by the scanner evaluator.

Lifecycle:
  1. Pre-market warmup:
       state = SymbolState.from_history(symbol, daily_bars, spy_daily,
                                        sector_daily, bars_5m_history)
  2. Each 1-min bar arrives (from the streaming loop):
       state.on_bar(bar, spy_bar, sector_bar)
  3. Gate / trigger evaluation (Steps 4–5) reads indicator properties.
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import date
from typing import Optional

import pandas as pd

from scanner.indicators.atr import wilder_atr
from scanner.indicators.chart_quality import chart_quality as compute_chart_quality
from scanner.indicators.ema_sma import SeededEMA, ema, sma
from scanner.indicators.rvol import build_volume_profile, compute_rvol
from scanner.indicators.rrs import D1_LENGTH, M5_LENGTH, rrs as compute_rrs, rrs_raw as compute_rrs_raw
from scanner.indicators.vwap import session_vwap

log = logging.getLogger(__name__)

_ATR_D1_LEN = D1_LENGTH   # Wilder ATR length for daily (same as RRS window)
_ATR_EXTENSION_D1_LEN = 14
_EMA8_D1_SPAN = 8
_SMA_LENS = (50, 100, 200)
_EMA_SHORT = 3             # intraday short EMA (for 3/9 crossover trigger)
_EMA_LONG  = 9             # intraday long EMA
_EMA_MED   = 8             # intraday medium EMA (new triggers: 3/8 cross, 8/21 trend)
_EMA_TREND = 21            # intraday trend EMA
_M5_DEQUE_SIZE   = 120          # 10 h of 5-min bars — a full session with room to spare
_RRS_TAIL        = 6            # 5-min bars of RRS history kept for rrs_slope (30 min)
_M5_RRS_MIN_BARS = M5_LENGTH    # raw M5 RRS available after 1 h; was M5_LENGTH*2 (smooth)
_RR_1M_RING = 30                          # RTH 1-min bars kept for 5-bar stop + 15-min momentum
                                          # + vwap_crosses_30m.
                                          # Safe to raise: every existing consumer only reads the
                                          # tail (last 5 / last 16), so more history retained changes
                                          # no existing computed value -- verified against the ring
                                          # size, not assumed.
_RTH_OPEN_MIN = 9 * 60 + 30               # 09:30 ET in minutes since midnight
_RTH_CLOSE_MIN = 16 * 60                  # 16:00 ET; bars at or after this are postmarket


class SymbolState:
    """Holds all per-symbol state for one scanner session."""

    # ── Construction ─────────────────────────────────────────────────────────

    def __init__(
        self,
        symbol: str,
        prior_high: float,
        prior_low: float,
        prior_close: float,
        atr_d1: float,
        rrs_d1: float,
        rrs_sector_d1: Optional[float],
        sma_50: Optional[float],
        sma_100: Optional[float],
        sma_200: Optional[float],
        ema_8_d1: float,
        chart_quality: float,
        volume_profile: pd.Series,
        daily_highs_60d: pd.Series,
        daily_lows_60d: pd.Series,
        prior_open: Optional[float] = None,
        adv20: Optional[float] = None,
        atr_14_d1: Optional[float] = None,
    ) -> None:
        # Daily state (set at warmup, does not change during session)
        self.symbol = symbol
        self.prior_high = prior_high
        self.prior_low = prior_low
        self.prior_close = prior_close
        # System-setup additions
        self.prior_open = prior_open
        self.adv20 = adv20
        self.atr_d1 = atr_d1
        self.atr_14_d1 = atr_14_d1
        self.rrs_d1 = rrs_d1
        self.rrs_sector_d1 = rrs_sector_d1
        self.sma_50 = sma_50
        self.sma_100 = sma_100
        self.sma_200 = sma_200
        self.ema_8_d1 = ema_8_d1
        self.chart_quality = chart_quality
        self.volume_profile = volume_profile
        self.daily_highs_60d = daily_highs_60d
        self.daily_lows_60d = daily_lows_60d

        # Mutable intraday state (reset each session)
        self._reset_intraday()

    @classmethod
    def from_history(
        cls,
        symbol: str,
        daily_bars: pd.DataFrame,
        spy_daily: pd.DataFrame,
        sector_daily: Optional[pd.DataFrame] = None,
        bars_5m_history: Optional[pd.DataFrame] = None,
    ) -> "SymbolState":
        """Build SymbolState from daily history at pre-market warmup.

        Args:
            symbol:           ticker symbol
            daily_bars:       OHLCV daily history for the stock (UTC index)
            spy_daily:        SPY daily history (same schema)
            sector_daily:     sector ETF daily history (optional)
            bars_5m_history:  ~20 days of 5-min bars for RVOL profile (optional)
        """
        stock, spy = daily_bars.align(spy_daily, join="inner", axis=0)

        # Prior session levels (last completed daily bar)
        prior = stock.iloc[-1]

        # ATR (daily, Wilder)
        atr_series = wilder_atr(stock["high"], stock["low"], stock["close"], _ATR_D1_LEN)
        atr_val = _scalar(atr_series.iloc[-1])
        atr14_series = wilder_atr(
            stock["high"], stock["low"], stock["close"], _ATR_EXTENSION_D1_LEN)
        atr14_last = atr14_series.iloc[-1]
        atr14_val = None if pd.isna(atr14_last) else _scalar(atr14_last)

        # SMAs
        sma_vals: dict[int, Optional[float]] = {}
        for n in _SMA_LENS:
            v = sma(stock["close"], n).iloc[-1]
            sma_vals[n] = None if pd.isna(v) else _scalar(v)

        # Daily EMA8 (used for chart quality extension check)
        ema8_series = ema(stock["close"], _EMA8_D1_SPAN)
        ema8_val = _scalar(ema8_series.iloc[-1])

        # D1 RRS vs SPY
        rrs_val = _scalar(compute_rrs(stock, spy, D1_LENGTH).iloc[-1])

        # D1 RRS vs sector ETF (optional)
        rrs_sector: Optional[float] = None
        if sector_daily is not None:
            s2, sec = daily_bars.align(sector_daily, join="inner", axis=0)
            v = compute_rrs(s2, sec, D1_LENGTH).iloc[-1]
            rrs_sector = None if pd.isna(v) else _scalar(v)

        # Chart quality score
        quality = compute_chart_quality(stock, ema8_series, atr_series)

        # RVOL volume profile (empty Series if no intraday history supplied)
        vol_profile: pd.Series = pd.Series(dtype=float)
        if bars_5m_history is not None and not bars_5m_history.empty:
            vol_profile = build_volume_profile(bars_5m_history)

        # Last 60 daily highs/lows for void/resistance gate
        window_60 = min(60, len(stock))
        daily_highs_60d = stock["high"].iloc[-window_60:].copy()
        daily_lows_60d  = stock["low"].iloc[-window_60:].copy()

        # System setups: prior-day open (for prior_day_chg_pct) + 20-day ADV
        prior_open_val = _scalar(prior["open"]) if "open" in prior else None
        adv20_val = None
        if "volume" in stock.columns and len(stock) > 0:
            v = stock["volume"].tail(20).mean()
            adv20_val = None if pd.isna(v) else _scalar(v)

        return cls(
            symbol=symbol,
            prior_high=_scalar(prior["high"]),
            prior_low=_scalar(prior["low"]),
            prior_close=_scalar(prior["close"]),
            prior_open=prior_open_val,
            adv20=adv20_val,
            atr_d1=atr_val,
            atr_14_d1=atr14_val,
            rrs_d1=rrs_val,
            rrs_sector_d1=rrs_sector,
            sma_50=sma_vals[50],
            sma_100=sma_vals[100],
            sma_200=sma_vals[200],
            ema_8_d1=ema8_val,
            chart_quality=quality,
            volume_profile=vol_profile,
            daily_highs_60d=daily_highs_60d,
            daily_lows_60d=daily_lows_60d,
        )

    # ── Session lifecycle ─────────────────────────────────────────────────────

    def close_session(self, close: float, high: float, low: float) -> None:
        """Record today's session close/high/low as the new prior-day levels.

        Call this at end-of-day (before reset_session) during replay so that
        tomorrow's gate checks use the correct prior-session reference levels.
        """
        self.prior_close = close
        self.prior_high = high
        self.prior_low = low

    # ── Intraday reset ────────────────────────────────────────────────────────

    def _reset_intraday(self) -> None:
        """Clear intraday state at the start of a new session."""
        self._cum_vol: float = 0.0
        self._vwap_bars: list[dict] = []
        self._vwap_value: Optional[float] = None
        self._high_of_day: float = float("-inf")
        self._low_of_day: float = float("inf")
        self._prev_hod: Optional[float] = None
        self._prev_lod: Optional[float] = None

        # VWAP cross / SR trigger: prev-bar snapshots
        self._prev_close: Optional[float] = None
        self._prev_vwap: Optional[float] = None
        self._last_close: Optional[float] = None

        # Incremental EMAs on 5-min bars
        self._ema_3: Optional[float] = None
        self._ema_9: Optional[float] = None
        self._prev_ema_3: Optional[float] = None
        self._prev_ema_9: Optional[float] = None
        self._ema_8:       Optional[float] = None
        self._ema_21:      Optional[float] = None
        self._prev_ema_8:  Optional[float] = None
        self._prev_ema_21: Optional[float] = None
        self._ema_trackers = {n: SeededEMA(n) for n in (_EMA_SHORT, _EMA_LONG, _EMA_MED, _EMA_TREND)}

        # Conviction: 10AM bar open (resets each session). The trigger's clean-run
        # check is a rolling last-N-bars window, so no
        # per-session "ever violated" flags are needed.
        self._open_10am:                Optional[float] = None

        # 5-min bar aggregation (ring-buffer — a full session of lookback)
        self._stock_5m: deque[dict] = deque(maxlen=_M5_DEQUE_SIZE)
        self._spy_5m: deque[dict] = deque(maxlen=_M5_DEQUE_SIZE)
        self._sector_5m: deque[dict] = deque(maxlen=_M5_DEQUE_SIZE)
        self._partial_stock_5m: Optional[dict] = None
        self._partial_spy_5m: Optional[dict] = None
        self._partial_sector_5m: Optional[dict] = None
        self._last_sector_bar_timestamp = None

        self._rrs_m5: Optional[float] = None
        self._rrs_sector_m5: Optional[float] = None
        # Cross-sectional opening-volume rank is assigned by LiveScanner once
        # the first RTH five-minute candle is complete across enough symbols.
        # The per-symbol opening candle/RVOL values themselves are derived from
        # this state's completed candle and historical volume profile below.
        self._opening_rvol_rank: Optional[float] = None
        self._opening_rvol_population: int = 0
        self._opening_rvol_coverage: float = 0.0
        # Last few per-bar M5 RRS values, oldest first, for the rrs_slope condition.
        # Free: they are the tail of the series _maybe_update_rrs_m5 already computes.
        self._rrs_m5_tail: list[float] = []

        # System-setup intraday state
        self._pm_high: Optional[float] = None       # premarket 04:00-09:29 high
        self._pm_low:  Optional[float] = None       # premarket low
        self._pm_vol:  float = 0.0                  # premarket volume (sum)
        self._pm_last: Optional[float] = None       # close of the latest premarket bar
        self._session_open: Optional[float] = None  # first RTH 1-min bar OPEN
        self._last_1m: deque[dict] = deque(maxlen=_RR_1M_RING)   # RTH 1-min bars, for 5-bar stop + 15m momentum

    # ── Live bar update ───────────────────────────────────────────────────────

    def on_bar(self, bar: dict, spy_bar: Optional[dict] = None,
               sector_bar: Optional[dict] = None) -> None:
        """Update all intraday state on a new 1-min bar close.

        Args:
            bar:     1-min bar dict (keys: timestamp, open, high, low, close, volume)
            spy_bar: matching SPY 1-min bar (optional; required for RRS_M5)
            sector_bar: latest matching sector-ETF bar (optional; sector RRS)
        """
        high = bar["high"]
        low = bar["low"]
        close = bar["close"]
        vol = bar["volume"]

        # Session state is RTH-anchored (09:30 ET onward). Premarket bars
        # (04:00-09:29) ONLY feed the premarket levels and return: they must not
        # touch VWAP, HOD/LOD, cumulative volume, or the 5-min deque.
        #   * RVOL: the volume profile (indicators/rvol.py) is RTH-only, so a
        #     premarket-inflated numerator overstated RVOL on every gapper.
        #   * VWAP / HOD / LOD: the setup definitions and run_live.py's own
        #     "9:30 onwards" comment all mean RTH session VWAP.
        # Both the live stream and the startup seeding replay deliver premarket
        # bars into this method, so this is the single place to gate them.
        et_min = _et_minutes(bar["timestamp"])
        if et_min < _RTH_OPEN_MIN:
            self._pm_high = high if self._pm_high is None else max(self._pm_high, high)
            self._pm_low  = low  if self._pm_low  is None else min(self._pm_low,  low)
            self._pm_vol += vol
            self._pm_last = close
            return
        if et_min >= _RTH_CLOSE_MIN:
            # Postmarket. Session VWAP, volume, HOD/LOD and the 5-min deque are
            # RTH values and freeze at the close.
            return

        if self._session_open is None:
            self._session_open = bar["open"]
        self._last_1m.append({
            "timestamp": bar["timestamp"], "open": bar["open"],
            "high": high, "low": low, "close": close, "volume": vol,
        })

        # Snapshot previous-bar values BEFORE updating state for this bar
        self._prev_close = self._last_close
        self._prev_vwap  = self.vwap       # VWAP before this bar's volume is added

        # Classic VWAP on completed RTH candles only. Keep the full session so
        # startup seeding and live updates use the same calculation and anchor.
        self._vwap_bars.append(self._last_1m[-1])
        vwap_frame = pd.DataFrame(self._vwap_bars)
        vwap_frame.index = pd.DatetimeIndex(vwap_frame["timestamp"])
        vwap_last = session_vwap(vwap_frame.high, vwap_frame.low,
                                 vwap_frame.close, vwap_frame.volume).iloc[-1]
        self._vwap_value = None if pd.isna(vwap_last) else float(vwap_last)
        self._cum_vol += vol

        # Record this bar's side of the (now fully updated) VWAP, for
        # vwap_crosses_30m (a chop measure, informational only, read nowhere
        # in gates/triggers).
        v = self.vwap
        self._last_1m[-1]["vwap_side"] = (close > v) if v is not None else None

        # Snapshot HOD/LOD before this bar extends them (used by hod_breakout trigger)
        self._prev_hod = self.high_of_day
        self._prev_lod = self.low_of_day

        # Intraday high / low
        self._high_of_day = max(self._high_of_day, high)
        self._low_of_day = min(self._low_of_day, low)

        # 5-min bar aggregation; EMAs updated on each completed 5-min bar close
        prev_5m_len = len(self._stock_5m)
        slot = _five_min_slot(bar["timestamp"])
        self._partial_stock_5m = _aggregate_into_slot(
            self._partial_stock_5m, self._stock_5m, bar, slot
        )
        stock_5m_completed = len(self._stock_5m) > prev_5m_len
        if stock_5m_completed:
            close_5m = self._stock_5m[-1]["close"]
            self._prev_ema_3 = self._ema_3
            self._prev_ema_9 = self._ema_9
            self._ema_3 = self._ema_trackers[_EMA_SHORT].push(close_5m)
            self._ema_9 = self._ema_trackers[_EMA_LONG].push(close_5m)

            # EMA 8/21 (for new triggers)
            self._prev_ema_8  = self._ema_8
            self._prev_ema_21 = self._ema_21
            self._ema_8  = self._ema_trackers[_EMA_MED].push(close_5m)
            self._ema_21 = self._ema_trackers[_EMA_TREND].push(close_5m)

            # Store snapshots in the completed bar dict for lookback-based triggers
            completed_bar = self._stock_5m[-1]
            # _prev_vwap was captured at the start of this on_bar call = VWAP at end of that bar
            completed_bar["vwap_snap"]  = self._prev_vwap
            completed_bar["ema9_snap"]  = self._ema_9
            completed_bar["ema21_snap"] = self._ema_21

        if spy_bar is not None:
            prev_spy_5m_len = len(self._spy_5m)
            spy_slot = _five_min_slot(spy_bar["timestamp"])
            self._partial_spy_5m = _aggregate_into_slot(
                self._partial_spy_5m, self._spy_5m, spy_bar, spy_slot
            )
            if stock_5m_completed or len(self._spy_5m) > prev_spy_5m_len:
                self._maybe_update_rrs_m5()

        if sector_bar is not None and sector_bar.get("timestamp") != self._last_sector_bar_timestamp:
            prev_sector_5m_len = len(self._sector_5m)
            sector_slot = _five_min_slot(sector_bar["timestamp"])
            self._partial_sector_5m = _aggregate_into_slot(
                self._partial_sector_5m, self._sector_5m, sector_bar, sector_slot
            )
            self._last_sector_bar_timestamp = sector_bar.get("timestamp")
            if stock_5m_completed or len(self._sector_5m) > prev_sector_5m_len:
                self._maybe_update_rrs_sector_m5()

        # Capture 10AM bar open (first 1-min bar at 10:00 ET)
        if self._open_10am is None:
            et = pd.Timestamp(bar["timestamp"])
            if et.tzinfo is None:
                et = et.tz_localize("UTC")
            et = et.tz_convert("America/New_York")
            if et.hour == 10 and et.minute == 0:
                self._open_10am = bar["open"]

        self._last_close = close

    def _maybe_update_rrs_m5(self) -> None:
        """Recompute M5 RRS from completed 5-min bars.

        Uses raw (un-smoothed) RRS so the signal is available after just 1 hour
        (M5_LENGTH = 12 bars) rather than 2 hours for the smoothed version.
        """
        if len(self._stock_5m) < _M5_RRS_MIN_BARS or len(self._spy_5m) < _M5_RRS_MIN_BARS:
            return
        s_df = _deque_to_df(self._stock_5m)
        b_df = _deque_to_df(self._spy_5m)
        series = compute_rrs_raw(s_df, b_df, M5_LENGTH)
        val = series.iloc[-1]
        self._rrs_m5 = None if pd.isna(val) else float(val)
        self._rrs_m5_tail = [float(v) for v in series.iloc[-(_RRS_TAIL + 1):] if not pd.isna(v)]

    def _maybe_update_rrs_sector_m5(self) -> None:
        """Recompute raw 5-minute RRS against the symbol's sector ETF."""
        if len(self._stock_5m) < _M5_RRS_MIN_BARS or len(self._sector_5m) < _M5_RRS_MIN_BARS:
            return
        s_df = _deque_to_df(self._stock_5m)
        b_df = _deque_to_df(self._sector_5m)
        series = compute_rrs_raw(s_df, b_df, M5_LENGTH)
        if series.empty:
            return
        val = series.iloc[-1]
        self._rrs_sector_m5 = None if pd.isna(val) else float(val)

    # ── Read-only properties ──────────────────────────────────────────────────

    @property
    def vwap(self) -> Optional[float]:
        return self._vwap_value

    @property
    def prev_vwap(self) -> Optional[float]:
        return self._prev_vwap

    @property
    def prev_close(self) -> Optional[float]:
        return self._prev_close

    @property
    def rvol(self) -> Optional[float]:
        if self.volume_profile.empty or not self._last_1m:
            return None
        # Minutes of RTH that _cum_vol covers: the latest 1-min bar is stamped at
        # its start, so it closes one minute later. Using the 5-min slot start
        # here compared today's volume against a shorter baseline interval.
        mins = _et_minutes(self._last_1m[-1]["timestamp"]) - _RTH_OPEN_MIN + 1
        return compute_rvol(self.volume_profile, self._cum_vol, mins)

    def _opening_5m(self) -> Optional[dict]:
        """Today's completed 09:30-09:35 ET candle, if available."""
        for candle in self._stock_5m:
            if candle.get("slot") == _RTH_OPEN_MIN:
                return candle
        return None

    @property
    def opening_candle_direction(self) -> Optional[float]:
        """+1 bullish, -1 bearish, 0 doji for the first RTH five-minute bar."""
        candle = self._opening_5m()
        if candle is None:
            return None
        opened = float(candle["open"])
        closed = float(candle["close"])
        return 1.0 if closed > opened else -1.0 if closed < opened else 0.0

    @property
    def opening_rvol_m5(self) -> Optional[float]:
        """First-five-minute volume divided by its historical slot average."""
        candle = self._opening_5m()
        if candle is None or self.volume_profile.empty or 0 not in self.volume_profile.index:
            return None
        expected = float(self.volume_profile.loc[0])
        if expected <= 0:
            return None
        return float(candle["volume"]) / expected

    def set_opening_rvol_rank(self, rank: Optional[float], population: int,
                              universe_size: int) -> None:
        """Store the scanner-wide opening-RVOL rank and its data coverage."""
        self._opening_rvol_rank = rank
        self._opening_rvol_population = int(population)
        self._opening_rvol_coverage = (
            float(population) / float(universe_size) if universe_size > 0 else 0.0)

    @property
    def opening_rvol_rank(self) -> Optional[float]:
        """Cross-sectional rank where 1 is the highest opening RVOL."""
        return self._opening_rvol_rank

    @property
    def opening_rvol_population(self) -> int:
        return self._opening_rvol_population

    @property
    def opening_rvol_coverage(self) -> float:
        return self._opening_rvol_coverage

    @property
    def session_volume(self) -> float:
        """Cumulative RTH volume so far this session.

        Public read of `_cum_vol`, which the RVOL calculation above already
        maintains. This is what a "daily volume > N" universe condition means:
        shares traded today, as opposed to the 20-day average (`adv20`).
        """
        return self._cum_vol

    @property
    def high_of_day(self) -> Optional[float]:
        return None if self._high_of_day == float("-inf") else self._high_of_day

    @property
    def low_of_day(self) -> Optional[float]:
        return None if self._low_of_day == float("inf") else self._low_of_day

    @property
    def prev_hod(self) -> Optional[float]:
        return self._prev_hod

    @property
    def prev_lod(self) -> Optional[float]:
        return self._prev_lod

    @property
    def ema_3(self) -> Optional[float]:
        return self._ema_3

    @property
    def ema_9(self) -> Optional[float]:
        return self._ema_9

    @property
    def prev_ema_3(self) -> Optional[float]:
        return self._prev_ema_3

    @property
    def prev_ema_9(self) -> Optional[float]:
        return self._prev_ema_9

    @property
    def rrs_m5(self) -> Optional[float]:
        return self._rrs_m5

    @property
    def rrs_sector_m5(self) -> Optional[float]:
        return self._rrs_sector_m5

    @property
    def rrs_m5_tail(self) -> list[float]:
        """Per-bar M5 RRS for the last few completed 5-min bars, oldest first."""
        return self._rrs_m5_tail

    @property
    def ema_8(self) -> Optional[float]:
        return self._ema_8

    @property
    def ema_21(self) -> Optional[float]:
        return self._ema_21

    @property
    def prev_ema_8(self) -> Optional[float]:
        return self._prev_ema_8

    @property
    def prev_ema_21(self) -> Optional[float]:
        return self._prev_ema_21

    @property
    def open_10am(self) -> Optional[float]:
        return self._open_10am

    @property
    def bars_1m_count(self) -> int:
        return int(self._cum_vol > 0)  # proxy; exact count not needed by evaluator

    @property
    def bars_5m_completed(self) -> int:
        return len(self._stock_5m)

    # ── System-setup read-only properties ───────────────────────────────────

    @property
    def pm_high(self) -> Optional[float]:
        return self._pm_high

    @property
    def pm_low(self) -> Optional[float]:
        return self._pm_low

    @property
    def pm_last(self) -> Optional[float]:
        """Close of the latest premarket bar seen this session."""
        return self._pm_last

    @property
    def pm_vol(self) -> float:
        """Total premarket (04:00-09:29 ET) volume seen this session."""
        return self._pm_vol

    @property
    def session_open(self) -> Optional[float]:
        """Open of the first RTH (09:30 ET+) 1-min bar."""
        return self._session_open

    @property
    def last_1m(self) -> list[dict]:
        """Recent RTH 1-min bars, oldest first (up to _RR_1M_RING)."""
        return list(self._last_1m)

    @property
    def gap_pct(self) -> Optional[float]:
        """(session_open - prior_close) / prior_close * 100."""
        if self._session_open is None or not self.prior_close:
            return None
        return (self._session_open - self.prior_close) / self.prior_close * 100.0

    @property
    def prior_day_chg_pct(self) -> Optional[float]:
        """(prior_close - prior_open) / prior_open * 100."""
        if not self.prior_open or self.prior_close is None:
            return None
        return (self.prior_close - self.prior_open) / self.prior_open * 100.0

    @property
    def rth_chg_pct(self) -> Optional[float]:
        """Percent move from the RTH session open to the last close."""
        if self._session_open is None or self._last_close is None or self._session_open == 0:
            return None
        return (self._last_close - self._session_open) / self._session_open * 100.0

    @property
    def mom_15m_pct(self) -> Optional[float]:
        """Percent change over the last 15 completed RTH 1-min bars (close vs close 15 bars ago)."""
        bars = self._last_1m
        if len(bars) < 16:
            return None
        ref = bars[-16]["close"]
        if not ref:
            return None
        return (bars[-1]["close"] - ref) / ref * 100.0

    @property
    def dist_vwap_pct(self) -> Optional[float]:
        """(price - vwap) / vwap * 100 using the last close."""
        v = self.vwap
        if v is None or v == 0 or self._last_close is None:
            return None
        return (self._last_close - v) / v * 100.0

    @property
    def day_range_pos(self) -> Optional[float]:
        """Position of last close within the RTH day range: 0 = LOD, 1 = HOD."""
        hod, lod = self.high_of_day, self.low_of_day
        if hod is None or lod is None or self._last_close is None or hod == lod:
            return None
        return (self._last_close - lod) / (hod - lod)

    # ── Additive alert context, no gate reads these ──

    @property
    def dist_ema9_pct(self) -> Optional[float]:
        """Signed % distance of the last close from the 5-min EMA9."""
        e9 = self._ema_9
        if e9 is None or e9 == 0 or self._last_close is None:
            return None
        return (self._last_close - e9) / e9 * 100.0

    @property
    def vwap_crosses_30m(self) -> int:
        """Count of 1-min closes that flipped side of session VWAP over the
        last 30 RTH bars (a chop measure). 0 if fewer than 2 bars have a
        known side yet -- never None, so it is always safe to log."""
        sides = [b["vwap_side"] for b in self._last_1m if b.get("vwap_side") is not None]
        return sum(1 for i in range(1, len(sides)) if sides[i] != sides[i - 1])

    def __repr__(self) -> str:
        return (
            f"SymbolState({self.symbol!r} rrs_d1={self.rrs_d1:.2f} "
            f"rvol={self.rvol} vwap={self.vwap})"
        )


# ── Module-level helpers ──────────────────────────────────────────────────────

def _scalar(v) -> float:
    """Convert numpy scalar to Python float."""
    return float(v)


def _et_minutes(timestamp) -> int:
    """Return minutes since midnight ET for a UTC (or naive-UTC) timestamp."""
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    et = ts.tz_convert("America/New_York")
    return et.hour * 60 + et.minute


def _five_min_slot(timestamp) -> int:
    """Return the 5-min slot (minutes since midnight ET) for a UTC timestamp."""
    return (_et_minutes(timestamp) // 5) * 5


def _aggregate_into_slot(
    partial: Optional[dict],
    completed: deque,
    bar: dict,
    slot: int,
) -> dict:
    """Merge a 1-min bar into the current partial 5-min bar.

    If `bar` belongs to a new slot, finalise the previous partial bar
    (push to `completed`) and start a fresh one.  Returns the updated partial.
    """
    if partial is None or partial["slot"] != slot:
        if partial is not None:
            completed.append(partial)
        partial = {
            "slot":      slot,
            "timestamp": bar["timestamp"],
            "open":      bar["open"],
            "high":      bar["high"],
            "low":       bar["low"],
            "close":     bar["close"],
            "volume":    bar["volume"],
        }
    else:
        partial["high"]   = max(partial["high"],  bar["high"])
        partial["low"]    = min(partial["low"],   bar["low"])
        partial["close"]  = bar["close"]
        partial["volume"] += bar["volume"]
    return partial


def _deque_to_df(bars: deque) -> pd.DataFrame:
    """Convert a deque of 5-min bar dicts to a DataFrame suitable for rrs()."""
    df = pd.DataFrame(list(bars))
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
    df = df.set_index("timestamp").sort_index()
    return df[["open", "high", "low", "close", "volume"]]
