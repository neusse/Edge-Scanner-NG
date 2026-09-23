"""Live scanner — pre-market warmup and per-bar evaluation loop.

Lifecycle:
  1. Instantiate LiveScanner with a symbol list, DataFeed, and AlertSink.
  2. Call warmup() with pre-fetched history DataFrames to build SymbolStates.
  3. Call connect() to subscribe to live 1-min bars (blocks until stopped).
  4. Call reset_session() at end-of-day to clear intraday state for next session.

Bar routing inside _on_bar():
  • SPY bar  → update SPY intraday state → recompute market regime
  • Other bar → update symbol state (passing latest SPY bar for M5 RRS) →
                run Evaluator → push any alerts to AlertSink
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Optional

import pandas as pd

from scanner.alert_sink import AlertSink
from scanner.data.interface import DataFeed
from scanner.conditions import ConditionCtx
from scanner.market import MarketRegime, classify_market
from scanner.profiles import BarProfileCache, ProfileResult, profile_stats
from scanner.recent_activity import RecentActivity, describe_check
from scanner.state import SymbolState
from scanner.timing import bar_timer, perf_counter_ns
from scanner.trigger_catalog import SymbolSeries

log = logging.getLogger(__name__)


class LiveScanner:
    """Orchestrates warmup, live bar routing, evaluation, and alerting."""

    def __init__(
        self,
        symbols: list[str],
        feed: DataFeed,
        sink: Optional[AlertSink] = None,
        sector_map: Optional[dict[str, str]] = None,
    ) -> None:
        """
        Args:
            symbols:    list of ticker symbols to scan (exclude SPY — added automatically)
            feed:       DataFeed implementation (Alpaca or fake for tests)
            sink:       optional general-purpose sink, cleared on session reset.
                        Alerts go out through attach_system()'s sink.
            sector_map: optional mapping of symbol -> sector-ETF symbol for stacked RS
        """
        self.symbols = [s.upper() for s in symbols]
        self.feed = feed
        self.sink = sink if sink is not None else AlertSink()
        self._sector_map: dict[str, str] = sector_map or {}
        self._sector_symbols = sorted(set(self._sector_map.values()) - {"SPY"})

        self._states: dict[str, SymbolState] = {}
        self._spy_daily_history = pd.DataFrame()
        self._symbol_daily_history: dict[str, pd.DataFrame] = {}
        self._sector_daily_history: dict[str, pd.DataFrame] = {}
        self._bars_5m_history: dict[str, pd.DataFrame] = {}
        self._bars_5m_session_limits: dict[str, int] = {}
        # Multi-timeframe candle rings, one per symbol. Owned here rather than
        # by CustomEvaluator because the universe conditions read them too, and
        # they must be available whether or not custom setups are attached. The
        # cost is not new: CustomEvaluator.on_bar already advanced these for
        # every symbol on every bar, before its own `not plan.keys` early out.
        self._series: dict[str, SymbolSeries] = {}
        # Universe profiles. None until attach_profiles(); when absent every
        # alert passes, so this is inert unless deliberately wired up.
        self._profiles = None
        self._spy_state: Optional[SymbolState] = None
        self._latest_spy_bar: Optional[dict] = None
        self._latest_sector_bars: dict[str, dict] = {}
        self._sector_session_bars: dict[str, dict] = {}
        self._regime: MarketRegime = MarketRegime.NEUTRAL

        # System setups from an optional engine plugin (attach_system). Every
        # evaluator runs inside this one process on the same states and bars,
        # so they all share ONE Alpaca stream (Alpaca allows a single
        # concurrent data websocket per account). Gets SPY's RTH % move for
        # relative strength vs SPY.
        self._system_evaluator = None
        self._system_sink: Optional[AlertSink] = None
        self._custom_sink: Optional[AlertSink] = None
        # A second, optional plugin evaluator (attach_de). Shares the system
        # sink but is a separate evaluator, so nothing about it can touch what
        # the first one emits.
        self._de_evaluator = None
        # Custom setups (universe + triggers composed in the dashboard). Runs
        # LAST, after every other evaluator, and emits through its own sink
        # with setup=<custom id>, custom=true. See scanner/custom_setups.py.
        self._custom_evaluator = None
        # What every setup did per symbol in the last few minutes, for the Setup
        # check window. Handed to each evaluator as it is attached.
        self.activity = RecentActivity()

    # ── System setups ────────────────────────────────────────────────────────

    def attach_system(self, evaluator, sink: AlertSink) -> None:
        """Attach a plugin's system-setup evaluator + sink to run on every non-SPY bar."""
        self._system_evaluator = evaluator
        self._system_sink = sink
        evaluator.activity = self.activity

    def attach_de(self, evaluator) -> None:
        """Attach a second plugin evaluator. It emits through the system sink, so
        attach_system() must have been called first; it has no feed of its own."""
        if self._system_sink is None:
            raise RuntimeError("attach_de() requires attach_system() first: it emits on the system feed")
        self._de_evaluator = evaluator
        evaluator.activity = self.activity

    def attach_custom(self, evaluator, sink: Optional[AlertSink] = None) -> None:
        """Attach a CustomEvaluator, preferring an explicit or shared sink."""
        if sink is None:
            sink = self._system_sink if self._system_sink is not None else self.sink
        self._custom_sink = sink
        self._custom_evaluator = evaluator
        evaluator.activity = self.activity
        # Hand it this scanner's series store so both read the same rings. Any
        # series the evaluator already built are merged in, then it stops owning
        # them: _on_bar advances them from here on.
        if getattr(evaluator, "owns_series", False):
            for sym, s in evaluator._series.items():
                self._series.setdefault(sym, s)
            evaluator._series = self._series
            evaluator.owns_series = False
        for sym, s in self._series.items():
            for tf, period in evaluator.plan.emas:
                s.want_ema(tf, period)

    # ── Warmup ────────────────────────────────────────────────────────────────

    def warmup(
        self,
        spy_daily: pd.DataFrame,
        symbol_daily: dict[str, pd.DataFrame],
        sector_daily: Optional[dict[str, pd.DataFrame]] = None,
        bars_5m: Optional[dict[str, pd.DataFrame]] = None,
        session_date: date | str | None = None,
    ) -> None:
        """Build SymbolStates from pre-fetched history DataFrames.

        Separating data-fetch from state-build keeps this method testable
        without a live DataFeed.

        Args:
            spy_daily:      SPY daily OHLCV DataFrame (UTC index)
            symbol_daily:   {symbol: daily_df} for every symbol in self.symbols
            sector_daily:   {etf_symbol: daily_df} for sector ETFs (optional)
            bars_5m:        {symbol: 5m_df} for RVOL profile (optional)
            session_date:   Exclude this date and later from cached warmup;
                            today's bars are replayed through seed_session_bar.
        """
        self._spy_daily_history = spy_daily.copy()
        self._symbol_daily_history = {
            symbol: frame.copy() for symbol, frame in symbol_daily.items()
        }
        self._sector_daily_history = {
            symbol: frame.copy() for symbol, frame in (sector_daily or {}).items()
        }
        cutoff = pd.Timestamp(session_date).date() if session_date is not None else None
        self._bars_5m_history = {}
        for symbol, frame in (bars_5m or {}).items():
            history = frame.copy()
            if cutoff is not None and not history.empty:
                index = pd.DatetimeIndex(history.index)
                if index.tz is None:
                    index = index.tz_localize("UTC")
                history = history.loc[index.tz_convert("America/New_York").date < cutoff]
            self._bars_5m_history[symbol] = history
        self._bars_5m_session_limits = {}
        for symbol, frame in self._bars_5m_history.items():
            if frame.empty:
                continue
            index = pd.DatetimeIndex(frame.index)
            if index.tz is None:
                index = index.tz_localize("UTC")
            self._bars_5m_session_limits[symbol] = len(
                set(index.tz_convert("America/New_York").date)
            )
        # SPY gets its own SymbolState for intraday VWAP / regime tracking
        self._spy_state = SymbolState.from_history("SPY", spy_daily, spy_daily)
        log.debug("SPY state built")

        built = 0
        for sym in self.symbols:
            daily = symbol_daily.get(sym)
            if daily is None or daily.empty:
                log.warning("No daily history for %s — skipping", sym)
                continue

            sector_sym = self._sector_map.get(sym)
            sec_df = (sector_daily or {}).get(sector_sym) if sector_sym else None
            bars5m = self._bars_5m_history.get(sym)

            self._states[sym] = SymbolState.from_history(
                sym,
                daily,
                spy_daily,
                sector_daily=sec_df,
                bars_5m_history=bars5m,
            )
            # Candle rings for the same symbol. seed_intraday fills 5/15/30/60
            # from the 5-min history; the 1- and 2-min rings are session-only by
            # design, so conditions on those timeframes have no baseline until
            # the session provides one.
            series = self._series.setdefault(sym, SymbolSeries(sym))
            series.seed_daily(daily)
            series.seed_intraday(bars5m)
            self._sync_ema_state(self._states[sym])
            built += 1

        log.info("Warmup complete: %d / %d symbols loaded", built, len(self.symbols))

        if not self._sector_map:
            log.info(
                "No sector_map provided — sector_rrs is informational only "
                "(gate removed); the sector RS score bonus will be 0. "
                "Pass sector_map={sym: etf} to enable the bonus + Gate Check value."
            )
        no_profile = [sym for sym, st in self._states.items() if st.volume_profile.empty]
        if no_profile:
            log.warning(
                "RVOL volume profile missing for %d symbol(s): %s … "
                "Pass bars_5m to warmup() to enable the rvol gate.",
                len(no_profile),
                ", ".join(no_profile[:10]) + (" …" if len(no_profile) > 10 else ""),
            )

    def attach_profiles(self, engine) -> None:
        """Attach a ProfileEngine so alerts are screened before they are emitted.

        The check runs post-trigger, pre-emit: a profile is consulted only on
        the handful of would-be alerts per bar, never on every symbol, so it
        costs effectively nothing against the per-bar budget.
        """
        self._profiles = engine

    def _passes_profile(self, alert: dict, state: SymbolState, bar: dict,
                        session: str, cache, source: str) -> bool:
        """The choke point. Returns True when the alert may be emitted.

        Every evaluator's alerts pass through here, so screening is uniform and
        no evaluator needed changing. Failing open on an internal error is
        deliberate: a bug in this layer must not silence the scanner.

        Args:
            source: which evaluator produced this, "system" or "custom".
                Passed explicitly rather than sniffed from the payload.
        """
        eng = self._profiles
        quote_book = getattr(self.feed, "quote_book", None)
        quote = quote_book.get(state.symbol) if quote_book is not None else None
        if quote is not None:
            # The exact observation used by a spread gate travels with the alert.
            # It is independent of the bar's market timestamp and price.
            alert["quote"] = quote
        if eng is None:
            return True
        try:
            if source == "custom":
                code = alert.get("setup", "")
                cp = eng.for_setup(code, self._custom_profile_ids.get(code))
            else:
                code = alert.get("setup", "")          # a system setup code
                cp = eng.for_setup(code)
            # Shared set first, then the setup's own. ANDed, so listing a
            # condition in both is not a conflict: the tighter threshold wins.
            params = (list(eng.params_for(self._custom_param_sets.get(code)))
                      + list(self._custom_params.get(code, ()))) if source == "custom" else []
            if (cp is None or cp.is_empty) and not params:
                return True
            ctx = ConditionCtx(state=state, series=self._series.get(state.symbol),
                               bar=bar, session=session,
                               direction=alert.get("direction"),
                               fundamentals=self._fundamentals_for(state.symbol),
                               regime=self._regime, quote=quote)
            res = eng.check(cp, ctx, cache)
            alert["universe_profile"] = res.to_json()
            alert["profile_hash"] = res.hash
            checks = list(res.checks)
            passed = res.passed
            if params:
                # The setup's own dynamic conditions: "what is it doing right
                # now", as opposed to the universe's "what kind of stock is it".
                # Evaluated even when the setup has no universe filter, and
                # folded into one stats record so the panel reports a single
                # blocked count per setup rather than two competing ones.
                pres = eng.check_conditions(params, ctx, cache)
                alert["parameters"] = pres.to_json()["checks"]
                checks += pres.checks
                passed = passed and pres.passed
            combined = ProfileResult(res.profile_id, res.name, res.hash, passed, checks)
            profile_stats.record(code or source, combined)
            if not passed:
                log.debug("screen blocked %s %s: %s", state.symbol, code,
                          "; ".join(f"{c.name}={c.reason}" for c in checks if not c.passed))
                why = [f"{describe_check(c)} ({res.name} universe)" for c in res.checks if not c.passed]
                if params:
                    why += [describe_check(c) for c in pres.checks if not c.passed]
                self.activity.add(state.symbol, bar.get("timestamp"), source, code,
                                  alert.get("direction", ""), "blocked",
                                  alert.get("entry_trigger") or alert.get("trigger", ""), why)
            return passed
        except Exception as exc:
            log.error("profile check failed for %s: %s", state.symbol, exc, exc_info=True)
            return True

    def _record_push(self, accepted, alert: dict, source: str, code: str):
        """Note what the sink did with an alert that passed the screen, and hand
        its answer straight back. A sink returns False when its own don't-repeat
        window swallows the alert, which is otherwise invisible."""
        try:
            self.activity.add(alert.get("symbol", ""), alert.get("timestamp"), source, code,
                              alert.get("direction", ""), "sent" if accepted is not False else "repeat",
                              alert.get("entry_trigger") or alert.get("trigger", ""),
                              [] if accepted is not False else ["the feed already sent this alert on this stock recently"])
        except Exception:
            pass
        return accepted

    def _fundamentals_for(self, symbol: str) -> Optional[dict]:
        try:
            from scanner.fundamentals import get_cache
            return get_cache().get(symbol)
        except Exception:
            return None

    @property
    def _custom_profile_ids(self) -> dict[str, str]:
        """{custom setup id: profile id} from the live compiled plan."""
        ce = self._custom_evaluator
        if ce is None:
            return {}
        return {s["id"]: s.get("universe_profile") or "" for s in ce.plan.setups}

    @property
    def _custom_param_sets(self) -> dict[str, str]:
        """{custom setup id: parameter set id} from the live compiled plan."""
        ce = self._custom_evaluator
        if ce is None:
            return {}
        return {s["id"]: (s.get("parameter_set") or "") for s in ce.plan.setups}

    @property
    def _custom_params(self) -> dict[str, list[dict]]:
        """{custom setup id: its own dynamic conditions} from the live plan."""
        ce = self._custom_evaluator
        if ce is None:
            return {}
        return {s["id"]: (s.get("parameters") or []) for s in ce.plan.setups}

    # ── Series ────────────────────────────────────────────────────────────────

    def series(self, symbol: str) -> SymbolSeries:
        """The candle rings for one symbol, created on first use."""
        s = self._series.get(symbol)
        if s is None:
            s = self._series[symbol] = SymbolSeries(symbol)
            ce = self._custom_evaluator
            if ce is not None:
                for tf, period in ce.plan.emas:
                    s.want_ema(tf, period)
        return s

    def _advance_series(self, state: SymbolState, bar: dict) -> str:
        """Push one bar into the symbol's candle rings. Returns the session tag."""
        ts = pd.Timestamp(bar["timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        et = ts.tz_convert("America/New_York")
        vwap = state.vwap
        session = self.series(state.symbol).on_bar(
            bar, et.hour * 60 + et.minute, et.strftime("%Y-%m-%d"),
            float(vwap) if vwap is not None else None,
        )
        self._sync_ema_state(state)
        return session

    def _sync_ema_state(self, state: SymbolState) -> None:
        """Expose the alert series' completed five-minute EMA in Stock Info."""
        series = self.series(state.symbol)
        for period, name in ((3, "3"), (8, "8"), (9, "9"), (21, "21")):
            tracker = series.ema(5, period)
            setattr(state, f"_prev_ema_{name}", tracker.prev)
            setattr(state, f"_ema_{name}", tracker.value)
        if series.completed[5] and state._stock_5m:
            completed = state._stock_5m[-1]
            completed["ema9_snap"] = state.ema_9
            completed["ema21_snap"] = state.ema_21

    def seed_session_bar(self, bar: dict, spy_bar: Optional[dict] = None,
                         sector_bar: Optional[dict] = None) -> bool:
        """Replay one session bar without evaluating setups or emitting alerts.

        Mid-session startup must seed both SymbolState and the shared
        SymbolSeries. If only SymbolState is seeded, custom HOD/LOD triggers
        begin at the first live bar and mistake ordinary moves for day highs or
        lows even though the alert context holds the correct session levels.
        """
        state = self._states.get(str(bar.get("symbol") or ""))
        if state is None:
            return False
        before_opening_rvol = state.opening_rvol_m5
        state.on_bar(bar, spy_bar, sector_bar)
        if before_opening_rvol is None and state.opening_rvol_m5 is not None:
            self._refresh_opening_rvol_ranks()
        session = self._advance_series(state, bar)
        if self._custom_evaluator is not None:
            self._custom_evaluator.prime_bar(
                state,
                bar,
                session,
                spy_mom_15m=(self._spy_state.mom_15m_pct if self._spy_state else None),
            )
        return True

    def _refresh_opening_rvol_ranks(self) -> None:
        """Rank loaded symbols by first-five-minute RVOL, highest first.

        Equal values share a competition rank. Coverage travels with the rank
        so the condition can fail closed while the opening bars are incomplete.
        """
        values = {
            symbol: value
            for symbol, state in self._states.items()
            if (value := state.opening_rvol_m5) is not None
        }
        population = len(values)
        universe_size = len(self._states)
        rank_for_value: dict[float, int] = {}
        for rank, value in enumerate(sorted(values.values(), reverse=True), start=1):
            rank_for_value.setdefault(value, rank)
        for symbol, state in self._states.items():
            value = values.get(symbol)
            rank = float(rank_for_value[value]) if value is not None else None
            state.set_opening_rvol_rank(rank, population, universe_size)

    def _roll_session_if_new_day(self, bar: dict) -> bool:
        """Reset intraday state when the first bar of a new ET date arrives.

        Returns False for a bar that belongs to an EARLIER date than the session
        in progress (a late or replayed bar): the caller must drop it, because it
        would add yesterday's volume and prices to today's VWAP, volume and HOD/LOD.

        A process left running overnight used to carry yesterday's VWAP, volume,
        HOD/LOD and opening price into the new session. Startup seeding calls
        state.on_bar directly, so this only ever sees live bars.
        """
        try:
            ts = pd.Timestamp(bar["timestamp"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            day = ts.tz_convert("America/New_York").strftime("%Y-%m-%d")
        except Exception:
            return True
        prev = getattr(self, "_session_day", None)
        if prev is None:
            self._session_day = day
            return True
        if day < prev:
            log.warning("Dropped a %s bar dated %s: the session in progress is %s",
                        bar.get("symbol", "?"), day, prev)
            return False
        if day == prev:
            return True
        self._finalize_daily_context(prev)
        log.info("New session %s (was %s): daily context finalized and intraday state reset",
                 day, prev)
        self.reset_session()
        self._session_day = day
        if getattr(self, "_profiles", None) is not None:
            fundamentals = {
                symbol: value
                for symbol in self._states
                if (value := self._fundamentals_for(symbol)) is not None
            }
            self._profiles.resolve_members(self._states, fundamentals)
        return True

    @staticmethod
    def _append_completed_session(frame: pd.DataFrame, day: str,
                                  state: SymbolState) -> pd.DataFrame:
        if (state.session_open is None or state.high_of_day is None
                or state.low_of_day is None or state._last_close is None):
            return frame
        row = pd.DataFrame(
            {
                "open": [float(state.session_open)],
                "high": [float(state.high_of_day)],
                "low": [float(state.low_of_day)],
                "close": [float(state._last_close)],
                "volume": [float(state.session_volume)],
            },
            index=pd.DatetimeIndex([pd.Timestamp(day, tz="UTC")]),
        )
        combined = pd.concat([frame, row]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        return combined.tail(max(1, len(frame)))

    @staticmethod
    def _append_completed_5m(frame: Optional[pd.DataFrame],
                             state: SymbolState,
                             session_limit: Optional[int] = None) -> pd.DataFrame:
        bars = list(state._stock_5m)
        if state._partial_stock_5m is not None:
            bars.append(state._partial_stock_5m)
        if not bars:
            return frame.copy() if frame is not None else pd.DataFrame()
        completed = pd.DataFrame(bars)
        completed["timestamp"] = pd.to_datetime(completed["timestamp"], utc=True)
        completed = completed.set_index("timestamp").sort_index()
        completed = completed[["open", "high", "low", "close", "volume"]]
        if frame is None or frame.empty:
            return completed
        combined = pd.concat([frame, completed]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        if session_limit:
            et_days = pd.DatetimeIndex(combined.index).tz_convert("America/New_York").date
            keep_days = set(sorted(set(et_days))[-session_limit:])
            combined = combined[[day in keep_days for day in et_days]]
        return combined

    @staticmethod
    def _append_reference_session(frame: pd.DataFrame, day: str,
                                  completed: dict) -> pd.DataFrame:
        row = pd.DataFrame(
            {key: [float(completed[key])]
             for key in ("open", "high", "low", "close", "volume")},
            index=pd.DatetimeIndex([pd.Timestamp(day, tz="UTC")]),
        )
        combined = pd.concat([frame, row]).sort_index()
        combined = combined[~combined.index.duplicated(keep="last")]
        return combined.tail(max(1, len(frame)))

    def _update_sector_session(self, symbol: str, bar: dict) -> None:
        ts = pd.Timestamp(bar["timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        et = ts.tz_convert("America/New_York")
        minute = et.hour * 60 + et.minute
        if not 9 * 60 + 30 <= minute < 16 * 60:
            return
        current = self._sector_session_bars.get(symbol)
        if current is None:
            self._sector_session_bars[symbol] = {
                "open": float(bar["open"]),
                "high": float(bar["high"]),
                "low": float(bar["low"]),
                "close": float(bar["close"]),
                "volume": float(bar["volume"]),
            }
            return
        current["high"] = max(current["high"], float(bar["high"]))
        current["low"] = min(current["low"], float(bar["low"]))
        current["close"] = float(bar["close"])
        current["volume"] += float(bar["volume"])

    def _finalize_daily_context(self, day: str) -> None:
        """Fold the completed live session into the same daily inputs as warmup."""
        if not hasattr(self, "_states") or not self._states:
            return

        if self._spy_state is not None and not self._spy_daily_history.empty:
            self._spy_daily_history = self._append_completed_session(
                self._spy_daily_history, day, self._spy_state)

        old_states = self._states
        for symbol, state in old_states.items():
            history = self._symbol_daily_history.get(symbol)
            if history is None or history.empty:
                continue
            self._symbol_daily_history[symbol] = self._append_completed_session(
                history, day, state)
            self._bars_5m_history[symbol] = self._append_completed_5m(
                self._bars_5m_history.get(symbol), state,
                self._bars_5m_session_limits.get(symbol))

        for symbol, completed in self._sector_session_bars.items():
            history = self._sector_daily_history.get(symbol)
            if history is None or history.empty:
                continue
            self._sector_daily_history[symbol] = self._append_reference_session(
                history, day, completed)

        if self._spy_daily_history.empty:
            return
        self._spy_state = SymbolState.from_history(
            "SPY", self._spy_daily_history, self._spy_daily_history)

        rebuilt: dict[str, SymbolState] = {}
        for symbol, history in self._symbol_daily_history.items():
            if history.empty:
                continue
            sector_symbol = self._sector_map.get(symbol)
            sector_history = self._sector_daily_history.get(sector_symbol)
            bars_5m = self._bars_5m_history.get(symbol)
            state = SymbolState.from_history(
                symbol,
                history,
                self._spy_daily_history,
                sector_daily=sector_history,
                bars_5m_history=bars_5m,
            )
            if (bars_5m is None or bars_5m.empty) and symbol in old_states:
                state.volume_profile = old_states[symbol].volume_profile.copy()
            rebuilt[symbol] = state

            series = self.series(symbol)
            series.seed_daily(history)
            self._sync_ema_state(state)
            # The refreshed daily frame already contains `day`; prevent the
            # first new-session bar from appending the same extremes again.
            series.session_date = None

        self._states = rebuilt
        log.info("Daily context finalized for %d/%d symbols", len(rebuilt), len(self.symbols))

    # ── Bar routing ───────────────────────────────────────────────────────────

    def _on_bar(self, bar: dict) -> None:
        """Process one incoming 1-min bar.

        Called by the feed's streaming callback.  All state mutations and
        evaluations happen synchronously inside this method.

        Args:
            bar: dict with keys symbol, timestamp, open, high, low, close, volume
        """
        symbol: str = bar.get("symbol", "")
        if not self._roll_session_if_new_day(bar):
            return

        if symbol == "SPY":
            if self._spy_state is None:
                return
            self._spy_state.on_bar(bar)
            self._latest_spy_bar = bar
            self._regime = classify_market(bar["close"], self._spy_state.vwap)
            log.debug("SPY: close=%.2f vwap=%.2f regime=%s",
                      bar["close"], self._spy_state.vwap or 0, self._regime.value)
            return

        # Sector ETFs are reference symbols on the same shared stream. Keep the
        # latest bar for every mapped stock; do not create a second connection.
        # If an ETF is also explicitly in the user's universe, it continues on
        # below and may be evaluated as a normal symbol too.
        if symbol in self._sector_symbols:
            self._latest_sector_bars[symbol] = bar
            self._update_sector_session(symbol, bar)

        state = self._states.get(symbol)
        if state is None:
            return

        # Capacity instrumentation (scanner/timing.py). Off by default; when on,
        # a boolean read plus two perf_counter_ns calls per stage.
        _t = bar_timer.on
        if _t:
            _bar0 = perf_counter_ns()
            _t0 = _bar0

        sector_bar = self._latest_sector_bars.get(self._sector_map.get(symbol, ""))
        before_opening_rvol = state.opening_rvol_m5
        state.on_bar(bar, self._latest_spy_bar, sector_bar)
        if before_opening_rvol is None and state.opening_rvol_m5 is not None:
            self._refresh_opening_rvol_ranks()
        if _t:
            _t1 = perf_counter_ns(); bar_timer.record("state", _t1 - _t0); _t0 = _t1

        # Advance the candle rings once, here, so every consumer (custom setups
        # and the universe conditions) reads the same series. Must come after
        # state.on_bar: series.on_bar records the bar's VWAP, and this is the
        # same post-update value CustomEvaluator used to read.
        session = self._advance_series(state, bar)
        if _t:
            _t1 = perf_counter_ns(); bar_timer.record("series", _t1 - _t0); _t0 = _t1

        # What the existing evaluators fired on this bar, for custom setups that
        # reuse them as triggers ("setup:<code>").
        #
        # Recorded BEFORE the profile check, on purpose. ext_fired means "this
        # trigger fired", not "this alert was published". A custom setup using
        # setup:<code> as a trigger is screened by its OWN profile; letting that
        # system setup's profile also gate it would be surprising action at a distance.
        ext_fired: set[str] = set()
        # One memo per (symbol, bar) so setups sharing a profile, and profiles
        # sharing a condition, resolve it once. Dropped when the bar is done.
        pcache = BarProfileCache()

        if self._system_evaluator is not None and self._system_sink is not None:
            try:
                spy_chg = self._spy_state.rth_chg_pct if self._spy_state is not None else None
                spy_mom15 = self._spy_state.mom_15m_pct if self._spy_state is not None else None
                for alert in self._system_evaluator.on_bar(state, bar, spy_chg, spy_mom15):
                    ext_fired.add(f"setup:{alert['setup']}")
                    if not self._passes_profile(alert, state, bar, session, pcache, "system"):
                        continue
                    if self._record_push(self._system_sink.push(alert), alert, "system", alert["setup"]):
                        log.info(
                            "SYS ALERT %-6s  %-5s  %-8s  tier=%s  stop=%.2f (%.2f%%)  price=%.2f",
                            alert["symbol"], alert["direction"], alert["setup"],
                            alert.get("tier"), alert.get("suggested_stop") or 0,
                            alert.get("stop_pct") or 0, alert["price"],
                        )
            except Exception as exc:
                log.error("System evaluator error for %s: %s", symbol, exc, exc_info=True)
        if _t:
            _t1 = perf_counter_ns(); bar_timer.record("system", _t1 - _t0); _t0 = _t1

        # The second plugin evaluator runs after the first, never interleaved
        # with it. Its setups use their own codes, so their sink keys never
        # collide with the first evaluator's.
        if self._de_evaluator is not None and self._system_sink is not None:
            try:
                for alert in self._de_evaluator.on_bar(state, bar):
                    ext_fired.add(f"setup:{alert['setup']}")
                    if not self._passes_profile(alert, state, bar, session, pcache, "system"):
                        continue
                    if self._record_push(self._system_sink.push(alert), alert, "system", alert["setup"]):
                        log.info(
                            "DE ALERT  %-6s  %-5s  %-8s  slot=%s  stop=%.2f (%.2f%%)  price=%.2f",
                            alert["symbol"], alert["direction"], alert["setup"],
                            alert["context"].get("slot"), alert.get("suggested_stop") or 0,
                            alert.get("stop_pct") or 0, alert["price"],
                        )
            except Exception as exc:
                log.error("DE evaluator error for %s: %s", symbol, exc, exc_info=True)
        if _t:
            _t1 = perf_counter_ns(); bar_timer.record("de", _t1 - _t0); _t0 = _t1

        # Custom setups last: they may reuse anything above as a trigger and
        # never influence it. Best-effort: an error here never stops the rest.
        if self._custom_evaluator is not None and self._custom_sink is not None:
            try:
                spy_mom15 = self._spy_state.mom_15m_pct if self._spy_state is not None else None
                for alert in self._custom_evaluator.on_bar(state, bar, ext_fired, spy_mom15,
                                                           session=session, defer_commit=True):
                    if not self._passes_profile(alert, state, bar, session, pcache, "custom"):
                        continue
                    if self._record_push(self._custom_sink.push(alert), alert, "custom", alert["setup"]):
                        self._custom_evaluator.accept_alert(alert)
                        log.info("CS ALERT  %-6s  %-5s  %-18s  %s  price=%.2f",
                                 alert["symbol"], alert["direction"], alert["setup"],
                                 alert.get("trigger_note") or alert.get("entry_trigger"), alert["price"])
            except Exception as exc:
                log.error("Custom evaluator error for %s: %s", symbol, exc, exc_info=True)
        if _t:
            _end = perf_counter_ns()
            bar_timer.record("custom", _end - _t0)
            # Attribute this symbol's whole cost to its bar minute. The sum over
            # a minute is the number that has to fit inside the bar cadence.
            bar_timer.record_bar(str(bar.get("timestamp", ""))[:16], _end - _bar0)

    def ranked_symbols(self) -> list[str]:
        """Loaded symbols, most liquid first (20-day average dollar volume).

        A provider that caps its best data tier, as Schwab does at 300 real-bar
        symbols, is served in this order, so the names that matter get the best
        data. The universe loader returns symbols alphabetically, so the order
        has to be made here.
        """
        def _dollar_volume(sym: str) -> float:
            st = self._states[sym]
            try:
                v = float(st.adv20 or 0.0) * float(st.prior_close or 0.0)
            except (TypeError, ValueError):
                return 0.0
            return v if v == v else 0.0
        return sorted(self._states.keys(), key=lambda sym: (-_dollar_volume(sym), sym))

    # ── Connection ────────────────────────────────────────────────────────────

    def connect(self) -> None:
        """Subscribe to 1-min bars and block until the stream ends.

        Calls feed.subscribe_minute_bars() which is expected to block.
        To stop cleanly, call feed.stop_stream() from a signal handler or
        another thread.
        """
        if not self._states:
            log.warning("connect() called before warmup — no symbols loaded")
        all_symbols = ["SPY"] + [sym for sym in self.ranked_symbols() if sym != "SPY"]
        all_symbols += [sym for sym in getattr(self, "_sector_symbols", ()) if sym not in all_symbols]
        log.info("Connecting to live feed for %d symbols", len(all_symbols))
        self.feed.subscribe_minute_bars(all_symbols, self._on_bar)

    # ── Session reset ─────────────────────────────────────────────────────────

    def reset_session(self) -> None:
        """Reset all intraday state for the next trading session.

        Call this after market close to prepare for tomorrow's session.
        """
        # An explicit reset (replay, the demo) already did the rollover: forget the
        # date so the next bar starts a session instead of triggering a second reset.
        self._session_day = None
        for state in self._states.values():
            state._reset_intraday()
        if self._spy_state is not None:
            self._spy_state._reset_intraday()
        self._latest_spy_bar = None
        self._latest_sector_bars.clear()
        self._sector_session_bars.clear()
        self._regime = MarketRegime.NEUTRAL
        self.sink.clear()
        if self._system_evaluator is not None:
            self._system_evaluator.reset()
        if self._system_sink is not None:
            self._system_sink.clear()
        if self._custom_sink is not None and self._custom_sink is not self._system_sink:
            self._custom_sink.clear()
        if self._de_evaluator is not None:
            self._de_evaluator.reset()
        if self._custom_evaluator is not None:
            self._custom_evaluator.reset()
        try:
            from scanner.settings import gate_stats
            gate_stats.reset()
        except Exception:
            pass
        profile_stats.reset()
        # Static membership was resolved against yesterday's adv20 / ATR /
        # prior close. Those are only rebuilt at the next warmup, so drop the
        # member sets rather than carry a stale screen into a new session.
        if self._profiles is not None:
            self._profiles.invalidate_members()
        log.info("Session reset complete")
