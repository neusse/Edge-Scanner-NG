"""Fail-closed setup readiness for dynamically admitted symbols.

The normal startup universe is warmed as one cohort.  A symbol admitted in the
middle of a session is different: every enabled setup must prove that the data
it reads exists before the symbol may enter that setup's evaluation plan.
Readiness is availability, not a trading signal; thresholds may currently fail
and the setup can still be ready to evaluate future bars.
"""
from __future__ import annotations

from typing import Any, Optional

import pandas as pd

from scanner.conditions import CATALOG as CONDITION_CATALOG, ConditionCtx
from scanner.trigger_catalog import BY_ID as TRIGGER_CATALOG, level_value


_CANDLE_NEEDS = {
    "bull_candle_close": 1, "bear_candle_close": 1, "doji": 1,
    "upper_shadow": 1, "lower_shadow": 1,
    "bull_engulfing": 2, "bear_engulfing": 2,
    "bull_harami": 2, "bear_harami": 2, "inside_bar": 2,
    "double_inside_bar": 3,
}


def _component(available: bool, reason: str, **evidence: Any) -> dict:
    return {"available": bool(available), "reason": reason, **evidence}


def _tf(trigger: dict) -> Optional[int]:
    params = trigger.get("params") or {}
    if "tf" in params:
        try:
            return int(params["tf"])
        except (TypeError, ValueError):
            return None
    numeric = []
    for option in trigger.get("options") or []:
        try:
            numeric.append(int(option))
        except (TypeError, ValueError):
            pass
    return numeric[0] if numeric else None


def _candle_need(trigger: dict) -> int:
    tid = trigger.get("id", "")
    params = trigger.get("params") or {}
    if tid in _CANDLE_NEEDS:
        return _CANDLE_NEEDS[tid]
    if tid in {"break_recent_high", "break_recent_low", "near_last_high", "near_last_low",
               "reject_last_high", "reject_last_low", "failed_swing_high", "failed_swing_low"}:
        return max(1, int(params.get("lookback", 5)))
    if tid in {"range_break", "running", "volume_spike"}:
        return max(2, int(params.get("lookback", 10)) + 1)
    if tid == "consec_candles":
        return max(1, int(params.get("count", 3)))
    if tid in {"vwap_v", "vwap_support", "vwap_resistance"}:
        return 2
    if tid == "back_to_ema":
        return max(2, int(params.get("lookback", 3)) + 1)
    if tid in {"ema_cross_ema", "cross_above", "cross_below", "through_vwap"}:
        return 2
    if tid.startswith("ta_"):
        return max(2, int(params.get("length", 14)) + 1)
    return 0


def _trigger_reasons(scanner: Any, state: Any, series: Any, trigger: dict) -> list[str]:
    tid = str(trigger.get("id") or "")
    definition = TRIGGER_CATALOG.get(tid)
    if definition is None:
        return [f"unknown trigger {tid}"]
    if definition.source == "system" or tid.startswith("setup:"):
        return [f"{tid} is supplied by a system plugin whose inputs cannot be certified"]

    reasons: list[str] = []
    params = trigger.get("params") or {}
    tf = _tf(trigger)
    need = _candle_need(trigger)
    if tf is not None and need:
        have = len(series.candles.get(tf, ()))
        # One-minute rings live in m1; completed one-minute candles are also
        # retained, but m1 makes delayed/gapped session coverage explicit.
        if tf == 1:
            have = len(series.m1)
        if have < need:
            reasons.append(f"{tid} needs {need} completed {tf}-minute candles; {have} loaded")

    if tid == "hi_lo_60d":
        need_days = max(1, int(params.get("days", 60)))
        have_days = len(series.daily_highs)
        if have_days < need_days:
            reasons.append(f"N-day break needs {need_days} completed daily sessions; {have_days} loaded")
    elif tid == "hi_lo_52w" and len(series.daily_highs) < 252:
        reasons.append(f"52-week break needs 252 completed daily sessions; {len(series.daily_highs)} loaded")

    if tid in {"cross_above", "cross_below"}:
        for option in trigger.get("options") or []:
            if option in {"open", "prior_close", "vwap", "pm_high", "pm_low", "prior_high", "prior_low",
                          "ema9_d", "ema21_d", "ema50_d", "sma50_d", "sma100_d", "sma200_d"} \
                    or str(option).startswith("ema"):
                if level_value(series, state, str(option)) is None:
                    reasons.append(f"level {option} is unavailable")

    if tid == "ema_cross_ema":
        tf = int(params.get("tf", 1))
        for period in (int(params.get("fast", 3)), int(params.get("slow", 9))):
            if series.ema(tf, period).value is None:
                reasons.append(f"EMA({period}) on {tf}-minute candles is not warm")
    elif tid == "back_to_ema":
        option = str((trigger.get("options") or [""])[0])
        if option.startswith("ema") and level_value(series, state, option) is None:
            reasons.append(f"level {option} is unavailable")

    if tid in {"orb_breakout", "orb_breakdown", "orb_trade_cross"}:
        for option in trigger.get("options") or ["5"]:
            try:
                interval = int(option)
            except (TypeError, ValueError):
                continue
            day = series.session_date
            opening = scanner._opening_ranges.completed(state.symbol, day, interval) if day else None
            if opening is None:
                reasons.append(f"{interval}-minute opening range is not complete")
        if tid == "orb_trade_cross" and not getattr(scanner.feed, "supports_trade_updates", False):
            reasons.append("the feed has no live trade updates")

    if tid == "gap" and series.session_open() is None:
        reasons.append("session open is unavailable")
    if tid in {"hod", "near_hod"} and series.day_high is None:
        reasons.append("regular-session high/low is unavailable")
    return reasons


def _condition_reasons(scanner: Any, state: Any, series: Any, setup: dict,
                       conditions: list[dict]) -> list[str]:
    if not conditions:
        return []
    quote_book = getattr(scanner.feed, "quote_book", None)
    quote = quote_book.get(state.symbol) if quote_book is not None else None
    bar = scanner._last_symbol_bar.get(state.symbol)
    fundamentals = scanner._fundamentals_for(state.symbol)
    directions = [setup.get("direction")] if setup.get("direction") in {"long", "short"} else ["long", "short"]
    reasons: list[str] = []
    for condition in conditions:
        cid = str(condition.get("id") or "")
        definition = CONDITION_CATALOG.get(cid)
        if definition is None:
            reasons.append(f"unknown condition {cid}")
            continue
        values = []
        for direction in directions:
            ctx = ConditionCtx(state=state, series=series, bar=bar, session="rth",
                               direction=direction, fundamentals=fundamentals,
                               regime=scanner._regime, quote=quote)
            try:
                values.append(definition.resolve(ctx, condition.get("option", ""),
                                                 condition.get("params") or {}))
            except Exception:
                values.append(None)
        if all(value is None for value in values):
            reasons.append(f"{definition.name} input is unavailable")
    return reasons


def assess(scanner: Any, symbol: str) -> dict:
    """Return explicit component and per-setup readiness for one symbol."""
    symbol = str(symbol).upper()
    state = scanner._states.get(symbol)
    series = scanner._series.get(symbol)
    daily = scanner._symbol_daily_history.get(symbol)
    intraday = scanner._bars_5m_history.get(symbol)
    session = list(series.m1) if series is not None else []
    sector_symbol = scanner._sector_map.get(symbol)
    sector_available = bool(
        sector_symbol and scanner._sector_daily_history.get(sector_symbol) is not None
        and not scanner._sector_daily_history[sector_symbol].empty
    )
    quote_book = getattr(scanner.feed, "quote_book", None)
    quote = quote_book.get(symbol) if quote_book is not None else None
    ema_total = len(series.emas) if series is not None else 0
    ema_ready = sum(1 for tracker in series.emas.values() if tracker.value is not None) if series is not None else 0
    components = {
        "daily": _component(bool(state is not None and daily is not None and not daily.empty),
                            "daily history loaded" if daily is not None and not daily.empty else "daily history missing",
                            rows=0 if daily is None else len(daily)),
        "intraday": _component(bool(intraday is not None and not intraday.empty),
                               "five-minute history loaded" if intraday is not None and not intraday.empty else "five-minute history missing",
                               rows=0 if intraday is None else len(intraday)),
        "session": _component(bool(state is not None and series is not None and session),
                              "current session synchronized" if session else "current session has no complete minute",
                              rows=len(session)),
        "sector": _component(sector_available,
                             f"{sector_symbol} context loaded" if sector_available else "sector mapping or history unavailable",
                             symbol=sector_symbol),
        "quote": _component(bool(quote), "Level One quote observed" if quote else "no Level One quote observed"),
        "indicators": _component(ema_ready == ema_total,
                                 "configured indicators warm" if ema_ready == ema_total else "one or more configured indicators are not warm",
                                 ready=ema_ready, configured=ema_total),
    }

    evaluator = scanner._custom_evaluator
    setup_rows: list[dict] = []
    ready_ids: list[str] = []
    if evaluator is not None and state is not None and series is not None:
        profiles = scanner._profiles
        for setup in evaluator.plan.setups:
            setup_id = str(setup.get("id") or "")
            reasons: list[str] = []
            status = "ready"
            cp = profiles.for_setup(setup_id, setup.get("universe_profile")) if profiles is not None else None
            if cp is not None and cp.static:
                ctx = ConditionCtx(state=state, series=series, bar=scanner._last_symbol_bar.get(symbol),
                                   fundamentals=scanner._fundamentals_for(symbol))
                checks = []
                for condition in cp.static:
                    definition = CONDITION_CATALOG.get(condition.get("id"))
                    try:
                        value = definition.resolve(ctx, condition.get("option", ""), condition.get("params") or {}) if definition else None
                    except Exception:
                        value = None
                    if value is None:
                        reasons.append(f"{definition.name if definition else condition.get('id')} input is unavailable")
                    else:
                        from scanner.conditions import check as check_condition
                        checks.append(check_condition(condition, ctx))
                if reasons:
                    status = "unavailable"
                elif any(not item.passed for item in checks):
                    status = "filtered"
                    reasons.extend(item.reason for item in checks if not item.passed)

            if status == "ready":
                if not components["daily"]["available"]:
                    reasons.append(components["daily"]["reason"])
                if not components["session"]["available"]:
                    reasons.append(components["session"]["reason"])
                for trigger in setup.get("triggers") or []:
                    reasons.extend(_trigger_reasons(scanner, state, series, trigger))
                params = list(setup.get("parameters") or [])
                if profiles is not None:
                    params = profiles.params_for(setup.get("parameter_set")) + params
                reasons.extend(_condition_reasons(scanner, state, series, setup, params))
                status = "unavailable" if reasons else "ready"

            row = {"id": setup_id, "name": setup.get("name") or setup_id,
                   "status": status, "reasons": list(dict.fromkeys(reasons))}
            setup_rows.append(row)
            if status == "ready":
                ready_ids.append(setup_id)

    # Optional private/system evaluators do not expose a declarative input
    # contract.  They remain visible but unavailable rather than being guessed
    # ready from the public custom-setup vocabulary.
    from scanner.plugins import SYSTEM_SETUPS
    for setup in SYSTEM_SETUPS:
        setup_rows.append({
            "id": setup.code, "name": setup.name, "status": "unavailable",
            "reasons": ["system setup does not expose certifiable dynamic-admission requirements"],
        })

    components["enabled_setups"] = _component(bool(ready_ids),
        f"{len(ready_ids)} enabled setup(s) ready" if ready_ids else "no enabled setup has complete inputs",
        ready=len(ready_ids), configured=len(setup_rows))
    base_ready = components["daily"]["available"] and components["session"]["available"]
    return {"ready": bool(base_ready), "components": components, "setups": setup_rows,
            "ready_setup_ids": ready_ids}


__all__ = ["assess"]
