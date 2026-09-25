"""Global trigger (alert) catalog for custom setups.

A custom setup is "universe + a list of triggers". This module is the single
place that defines every trigger the engine can evaluate, plus the per-symbol
bar series the intraday triggers read from (1/2/5/15/30/60-minute candles,
clock-aligned to the session, seeded from history at warmup).

Two kinds of trigger live here:

  * native   -- computed in this module from the candle series, SymbolState
                levels and the daily seed (candle patterns, crossings, ORB,
                near/reject levels, momentum runs, ...).
  * system   -- one pass-through per system setup an installed engine plugin
                provides (scanner/plugins.py). They are NOT recomputed:
                LiveScanner tells the evaluator which of them fired on this bar,
                so a custom setup that uses one fires exactly when the system
                setup does.

Nothing here touches the existing evaluators, gates or setups.
"""
from __future__ import annotations

import json
import math
from collections import deque
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

import pandas as pd

from scanner.indicators.ema_sma import SeededEMA
from scanner.indicators.classic import calculate, candles_frame

# 3 exists for the candle-size choice on Crossing above / below. It is session-only
# like 1 and 2, and deliberately NOT in _ALL_TF, so the triggers that offer every
# timeframe as an option did not all grow a "3 Min" box nobody asked for.
TIMEFRAMES = (1, 2, 3, 5, 15, 30, 60)
TF_LABEL = {1: "1 Min", 2: "2 Min", 3: "3 Min", 5: "5 Min", 15: "15 Min", 30: "30 Min", 60: "60 Min"}
_RTH_OPEN = 9 * 60 + 30
_RTH_CLOSE = 16 * 60
_PRE_OPEN = 4 * 60
_M1_RING = 800            # 04:00-16:00 is 720 one-minute bars
_CANDLE_RING = 128        # completed candles kept per timeframe. Deepest read is a
                          # lookback of 50 (+1), and the largest intraday EMA is
                          # period 21, which converges in ~84 candles. 128 covers
                          # both with margin; 400 cost ~3x the memory for nothing.


# ── catalog definitions ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class OptionDef:
    key: str
    label: str
    direction: Optional[str] = None     # option implies a direction (high/low, above/below)


@dataclass(frozen=True)
class ParamDef:
    key: str
    label: str
    default: float
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    unit: str = ""
    desc: str = ""
    # When set, the only values allowed; the editor shows a dropdown and saving
    # snaps to the nearest. For candle sizes, where a free number box would
    # accept a 4-minute candle the scanner never builds.
    choices: tuple[float, ...] = ()
    # Optional words for each choice, same order. A numeric code with a label is
    # how a param picks a MODE (a unit, a method) without a second kind of param.
    choice_labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class TriggerDef:
    id: str
    name: str
    category: str
    desc: str
    direction: str                       # long | short | both | neutral ("both" = option decides)
    options: tuple[OptionDef, ...] = ()
    option_label: str = "Timeframe"
    params: tuple[ParamDef, ...] = ()
    sessions: tuple[str, ...] = ("pre", "rth")
    source: str = "native"               # native | system
    default_options: tuple[str, ...] = ()
    lifetime: str = "edge_rearm"        # event contract exposed to Setup Check clients
    event_semantics: Optional[str] = None

    def to_json(self) -> dict:
        return {
            "id": self.id, "name": self.name, "category": self.category, "desc": self.desc,
            "direction": self.direction, "option_label": self.option_label,
            "options": [{"key": o.key, "label": o.label, "direction": o.direction} for o in self.options],
            "params": [{"key": p.key, "label": p.label, "default": p.default, "min": p.min, "max": p.max,
                        "step": p.step, "unit": p.unit, "desc": p.desc, "choices": list(p.choices),
                        "choice_labels": list(p.choice_labels)}
                       for p in self.params],
            "sessions": list(self.sessions), "source": self.source,
            "default_options": list(self.default_options),
            "lifetime": self.lifetime,
            "eventSemantics": self.event_semantics,
        }


def _tf_options(*tfs: int) -> tuple[OptionDef, ...]:
    return tuple(OptionDef(str(t), TF_LABEL[t]) for t in tfs)


_ALL_TF = _tf_options(1, 2, 5, 15, 30, 60)

# How close to VWAP counts as a touch for VWAP support / resistance. Default 3% of
# daily ATR: a median 3-minute candle spans 3.8% of daily ATR (269 stocks,
# 2026-08-26), so a touch should be a little tighter than one candle. That is
# ~0.11% of price on a typical stock, near the old 0.1%, but it scales: ~0.06%
# on a slow 1.9% ATR name and ~0.26% on a fast 8.7% one.
_VWAP_TOUCH = (
    ParamDef("tol_pct", "Touch tolerance", 3.0, 0, 50, 0.5, "",
             "How close the candle must come to VWAP, in the unit chosen next to it."),
    ParamDef("tol_unit", "Measured in", 1, 0, 1, 1, "",
             "% of price is the same number for every stock. % of daily ATR scales it to how "
             "much the stock normally moves.",
             choices=(0.0, 1.0), choice_labels=("% of price", "% of daily ATR")),
)


def _move_unit(desc: str) -> ParamDef:
    """The "Measured in" picker shared by Range break and Running up / down. The
    key stays width_unit on both so one name means one thing across the catalog."""
    return ParamDef("width_unit", "Measured in", 0, 0, 2, 1, "", desc,
                    choices=(0.0, 1.0, 2.0),
                    choice_labels=("% of price", "x daily ATR", "x avg candle"))
_HL = (OptionDef("high", "High", "long"), OptionDef("low", "Low", "short"))
_UPDOWN = (OptionDef("up", "Up", "long"), OptionDef("down", "Down", "short"))
_ABOVE_BELOW = (OptionDef("above", "Above", "long"), OptionDef("below", "Below", "short"))

# Levels for "Crossing above / below". Key -> (label, resolver name)
LEVELS: tuple[OptionDef, ...] = (
    OptionDef("open", "Session open"),
    OptionDef("prior_close", "Prior close"),
    OptionDef("vwap", "VWAP"),
    OptionDef("ema20_2", "EMA(20) 2 min"),
    OptionDef("ema9_5", "EMA(9) 5 min"),
    OptionDef("ema21_5", "EMA(21) 5 min"),
    OptionDef("ema9_15", "EMA(9) 15 min"),
    OptionDef("ema21_15", "EMA(21) 15 min"),
    OptionDef("ema9_60", "EMA(9) 60 min"),
    OptionDef("ema21_60", "EMA(21) 60 min"),
    OptionDef("ema9_d", "EMA(9) daily"),
    OptionDef("ema21_d", "EMA(21) daily"),
    OptionDef("ema50_d", "EMA(50) daily"),
    OptionDef("sma50_d", "SMA(50) daily"),
    OptionDef("sma100_d", "SMA(100) daily"),
    OptionDef("sma200_d", "SMA(200) daily"),
    OptionDef("pm_high", "Premarket high"),
    OptionDef("pm_low", "Premarket low"),
    OptionDef("prior_high", "Prior day high"),
    OptionDef("prior_low", "Prior day low"),
)
_EMA_LEVELS = tuple(o for o in LEVELS if o.key.startswith("ema") and not o.key.endswith("_d"))

# Candle size for Crossing above / below. Default 1 keeps every existing setup on
# exactly the rule it had.
_CROSS_TF = ParamDef("tf", "Candle size", 1, 1, 15, 1, "min",
                     "Chart the cross is judged on: the close of a completed candle of this size.",
                     choices=(1.0, 3.0, 5.0, 15.0))




def _build_catalog() -> list[TriggerDef]:
    cat: list[TriggerDef] = []
    add = cat.append

    # ── Highs & lows ─────────────────────────────────────────────────────
    add(TriggerDef("hod", "High/Low of the day", "Highs & lows",
                   "New high or low of the regular session (09:30 onward).", "both", _HL, "Side",
                   sessions=("rth",), default_options=("high",)))
    add(TriggerDef("hod_ext", "High/Low of the day (pre/post-market)", "Highs & lows",
                   "New high or low of the day including the premarket session.", "both", _HL, "Side",
                   default_options=("high",)))
    add(TriggerDef("near_hod", "Near high/low of the day", "Highs & lows",
                   "Close approaches within one 20-period 1-min ATR of the prior HOD/LOD, while the current bar's high/low stays inside that level (touch allowed, wick-through excluded). Fires once per approach.",
                   "both", _HL, "Side", sessions=("rth",), default_options=("high",)))
    # Id kept as hi_lo_60d so saved setups load unchanged; Days defaults to 60.
    add(TriggerDef("hi_lo_60d", "N-day high/low", "Highs & lows",
                   "First intraday trade above the highest high (below the lowest low) of the last N completed sessions. "
                   "Each side fires at most once per trading day, so retreats and recrosses do not repeat the alert. "
                   "Needs at least N sessions of daily history loaded at startup; with fewer it "
                   "does not fire, rather than quietly using a shorter window.", "both", _HL, "Side",
                   params=(ParamDef("days", "Days", 60, 2, 252, 1, "sessions",
                                    "How many completed sessions the high or low is taken over."),),
                   sessions=("rth",), default_options=("high",), lifetime="first_breach_per_day"))
    add(TriggerDef("hi_lo_52w", "52-week high/low", "Highs & lows",
                   "First strict intraday trade beyond the highest high (lowest low) of up to 252 completed daily sessions; once per side per trading day.",
                   "both", _HL, "Side", sessions=("rth",), default_options=("high",), lifetime="first_breach_per_day"))
    add(TriggerDef("prior_day_break", "Prior day high/low break", "Highs & lows",
                   "First strict intraday trade beyond yesterday's high (long) or low (short); once per side per trading day.", "both", _HL, "Side", default_options=("high",), lifetime="first_breach_per_day"))
    add(TriggerDef("pm_break", "Premarket high/low break", "Highs & lows",
                   "First strict regular-session trade beyond the premarket high (long) or low (short); once per side per trading day.", "both", _HL, "Side",
                   sessions=("rth",), default_options=("high",), lifetime="first_breach_per_day"))
    add(TriggerDef("new_candle_high", "New candle high", "Highs & lows",
                   "The current candle trades above the high of the previous N candles of the timeframe. Once per candle.",
                   "long", _ALL_TF, params=(ParamDef("since", "Since candles", 1, 1, 20, 1, "candles",
                                                     "Compare against the highest high of this many previous candles."),),
                   default_options=("5",)))
    add(TriggerDef("new_candle_low", "New candle low", "Highs & lows",
                   "The current candle trades below the low of the previous N candles of the timeframe. Once per candle.",
                   "short", _ALL_TF, params=(ParamDef("since", "Since candles", 1, 1, 20, 1, "candles",
                                                      "Compare against the lowest low of this many previous candles."),),
                   default_options=("5",)))
    add(TriggerDef("break_recent_high", "Break over recent high", "Highs & lows",
                   "Price breaks over the latest swing high the chart formed (highest high of the last N completed candles).",
                   "long", _ALL_TF, params=(ParamDef("lookback", "Swing lookback", 5, 2, 50, 1, "candles"),),
                   default_options=("5",)))
    add(TriggerDef("break_recent_low", "Break under recent low", "Highs & lows",
                   "Price breaks under the latest swing low the chart formed (lowest low of the last N completed candles).",
                   "short", _ALL_TF, params=(ParamDef("lookback", "Swing lookback", 5, 2, 50, 1, "candles"),),
                   default_options=("5",)))
    add(TriggerDef("near_last_high", "Near last high", "Highs & lows",
                   "Close is within one 20-candle ATR of the latest swing high and the current bar's high does not exceed it. Touch allowed; wick-through excluded. Fires once per approach.",
                   "long", _ALL_TF, params=(ParamDef("lookback", "Swing lookback", 5, 2, 50, 1, "candles"),),
                   default_options=("15",)))
    add(TriggerDef("near_last_low", "Near last low", "Highs & lows",
                   "Close is within one 20-candle ATR of the latest swing low and the current bar's low does not fall below it. Touch allowed; wick-through excluded. Fires once per approach.",
                   "short", _ALL_TF, params=(ParamDef("lookback", "Swing lookback", 5, 2, 50, 1, "candles"),),
                   default_options=("15",)))
    add(TriggerDef("reject_last_high", "Reject last high", "Highs & lows",
                   "A completed candle pokes above the latest swing high but closes back below it and red.",
                   "short", _ALL_TF, params=(ParamDef("lookback", "Swing lookback", 5, 2, 50, 1, "candles"),),
                   sessions=("rth",), default_options=("5",)))
    add(TriggerDef("reject_last_low", "Reject last low", "Highs & lows",
                   "A completed candle pokes below the latest swing low but closes back above it and green.",
                   "long", _ALL_TF, params=(ParamDef("lookback", "Swing lookback", 5, 2, 50, 1, "candles"),),
                   sessions=("rth",), default_options=("5",)))
    add(TriggerDef("orb_breakout", "Opening range breakout", "Highs & lows",
                   "Price breaks over the high of the first candle of the timeframe after the open. Once per day.",
                   "long", _ALL_TF, sessions=("rth",), default_options=("5",),
                   event_semantics="bar-close-cross"))
    add(TriggerDef("orb_trade_cross", "Opening range trade cross", "Highs & lows",
                   "First observed fresh Level One trade strictly crosses above a completed 5- or 15-minute opening range. Schwab only; once per day.",
                   "long", _tf_options(5, 15), sessions=("rth",), default_options=("5",),
                   event_semantics="trade-cross"))
    add(TriggerDef("orb_breakdown", "Opening range breakdown", "Highs & lows",
                   "Price breaks under the low of the first candle of the timeframe after the open. Once per day.",
                   "short", _ALL_TF, sessions=("rth",), default_options=("5",),
                   event_semantics="bar-close-cross"))

    # ── Candles ──────────────────────────────────────────────────────────
    add(TriggerDef("bull_candle_close", "Bullish candle close", "Candles", "A candle of the timeframe closes green.",
                   "long", _ALL_TF, default_options=("5",)))
    add(TriggerDef("bear_candle_close", "Bearish candle close", "Candles", "A candle of the timeframe closes red.",
                   "short", _ALL_TF, default_options=("5",)))
    add(TriggerDef("bull_engulfing", "Bullish engulfing candle", "Candles",
                   "Reversal pattern: a red candle body followed by a bigger green body that engulfs it.",
                   "long", _ALL_TF, default_options=("5",)))
    add(TriggerDef("bear_engulfing", "Bearish engulfing candle", "Candles",
                   "Reversal pattern: a green candle body followed by a bigger red body that engulfs it.",
                   "short", _ALL_TF, default_options=("5",)))
    add(TriggerDef("bull_harami", "Bullish harami candle", "Candles",
                   "A red candle body followed by a smaller green body inside it.", "long", _ALL_TF, default_options=("5",)))
    add(TriggerDef("bear_harami", "Bearish harami candle", "Candles",
                   "A green candle body followed by a smaller red body inside it.", "short", _ALL_TF, default_options=("5",)))
    add(TriggerDef("doji", "Doji candle", "Candles",
                   "Open and close nearly equal (body below a fraction of the range). Possible trend change.",
                   "neutral", _ALL_TF, params=(ParamDef("body_pct", "Max body", 10, 1, 50, 1, "% of range"),),
                   default_options=("5",)))
    add(TriggerDef("inside_bar", "Inside bar", "Candles",
                   "The completed candle's range sits entirely inside the previous candle's range.",
                   "neutral", _tf_options(5, 15, 30, 60), default_options=("15",)))
    add(TriggerDef("double_inside_bar", "Double inside bar", "Candles",
                   "Two consecutive inside bars: compression before a move.",
                   "neutral", _tf_options(5, 15, 30, 60), default_options=("15",)))
    add(TriggerDef("upper_shadow", "Upper shadow", "Candles",
                   "A completed candle with a long upper tail compared to its body (selling rejection).",
                   "short", _ALL_TF, params=(ParamDef("ratio", "Tail / body", 2.0, 1, 10, 0.5, "x"),),
                   default_options=("5",)))
    add(TriggerDef("lower_shadow", "Lower shadow", "Candles",
                   "A completed candle with a long lower tail compared to its body (buying rejection).",
                   "long", _ALL_TF, params=(ParamDef("ratio", "Tail / body", 2.0, 1, 10, 0.5, "x"),),
                   default_options=("5",)))
    add(TriggerDef("volume_spike", "Volume spike", "Candles",
                   "A completed candle's volume is at least X times the average of the previous N candles.",
                   "neutral", _ALL_TF, params=(ParamDef("ratio", "Min relative volume", 2.0, 1, 20, 0.5, "x"),
                                               ParamDef("lookback", "Average of", 10, 2, 50, 1, "candles")),
                   default_options=("5",)))
    add(TriggerDef("consec_candles", "Consecutive candles", "Candles",
                   "N consecutive green (long) or red (short) closed candles. Fires when the streak reaches N.",
                   "both", (OptionDef("green", "Green", "long"), OptionDef("red", "Red", "short")), "Color",
                   params=(ParamDef("count", "Streak", 3, 2, 20, 1, "candles"),
                           ParamDef("tf", "Timeframe (min)", 5, 1, 60, 1, "min", "1, 2, 5, 15, 30 or 60")), default_options=("green",)))

    # ── Crosses & levels ────────────────────────────────────────────────
    add(TriggerDef("cross_above", "Crossing above", "Crosses & levels",
                   "A candle closes above the selected level after the previous candle closed at "
                   "or below it. Candle size picks the chart: on 5 min it takes a 5-minute close "
                   "through the level, so a 1-minute poke that fails inside the candle does not count.",
                   "long", LEVELS, "Level", params=(_CROSS_TF,), default_options=("vwap",)))
    add(TriggerDef("cross_below", "Crossing below", "Crosses & levels",
                   "A candle closes below the selected level after the previous candle closed at "
                   "or above it. Candle size picks the chart: on 5 min it takes a 5-minute close "
                   "through the level, so a 1-minute poke that fails inside the candle does not count.",
                   "short", LEVELS, "Level", params=(_CROSS_TF,), default_options=("vwap",)))
    add(TriggerDef("vwap_v", "V off VWAP", "Crosses & levels",
                   "A sharp V into VWAP and straight back out. Price was well away from VWAP, "
                   "came to it without lingering, touched it, and the touch candle closed back "
                   "clearly on the right side as a strong candle. The difference from 'VWAP "
                   "acts as support' is the approach and the dwell: a stock that has been "
                   "sitting on VWAP for twenty minutes touches it on every candle, and this "
                   "refuses to call that a bounce.",
                   "both", (OptionDef("support", "Support (long)", "long"),
                            OptionDef("resistance", "Resistance (short)", "short")), "Side",
                   params=(ParamDef("tf", "Candle size", 5, 1, 60, 1, "min"),
                           ParamDef("lookback", "Window", 6, 3, 30, 1, "candles"),
                           ParamDef("away_atr", "Came from at least", 0.3, 0.05, 3, 0.05, "x ATR"),
                           ParamDef("band_atr", "Near VWAP means within", 0.1, 0.02, 1, 0.01, "x ATR"),
                           ParamDef("max_dwell", "Candles allowed near VWAP", 1, 0, 10, 1, "candles")), default_options=("support",)))
    add(TriggerDef("range_break", "Range break", "Highs & lows",
                   "Price has been trapped inside a tight range for N completed candles and "
                   "then leaves it on a 1-minute bar whose volume exceeds the range's per-minute average. The same shape as an opening-range break, "
                   "except the range is any N-candle consolidation rather than the first "
                   "candle of the day, so it can happen at any hour.",
                   "both", _UPDOWN, "Direction",
                   # max_range_pct keeps its key so saved setups load unchanged; since
                   # width_unit it holds a percent, an ATR multiple or a candle multiple.
                   params=(ParamDef("bars", "Candles in the range", 5, 2, 60, 1, "candles"),
                           ParamDef("max_range_pct", "Max range width", 1.5, 0.1, 20, 0.1, "",
                                    "How wide the range may be, in the unit chosen next to it."),
                           _move_unit("% of price is the same number for every stock. x daily ATR "
                                      "scales it to how much the stock normally moves in a day. "
                                      "x avg candle compares it with the stock's own recent candles, "
                                      "so it adjusts to a busy open and a quiet lunch."),
                           ParamDef("vol_mult", "Breakout volume", 1.5, 1, 10, 0.1, "x avg"),
                           ParamDef("tf", "Timeframe", 5, 1, 60, 1, "min")), default_options=("up",)))
    add(TriggerDef("ema_cross_ema", "EMA crosses EMA", "Crosses & levels",
                   "A faster EMA crosses a slower one on the chosen timeframe. Every other "
                   "cross in this catalog compares PRICE to a level; this one compares two "
                   "moving averages, as in the common 3/9 and 3/8 crossovers.",
                   "both", _UPDOWN, "Direction",
                   params=(ParamDef("fast", "Fast EMA", 3, 2, 200, 1, "period"),
                           ParamDef("slow", "Slow EMA", 9, 3, 200, 1, "period"),
                           ParamDef("tf", "Timeframe", 1, 1, 60, 1, "min")), default_options=("up",)))
    add(TriggerDef("through_vwap", "Through VWAP", "Crosses & levels",
                   "Price rushes through VWAP with a 1-min candle at least X times the average range of the last 10 candles.",
                   "both", _ABOVE_BELOW, "Side", params=(ParamDef("mult", "Candle size", 3.0, 1, 10, 0.5, "x avg range"),),
                   sessions=("rth",), default_options=("above",)))
    add(TriggerDef("vwap_support", "VWAP support hold", "Crosses & levels",
                   "A candle dips into a bounded VWAP touch band (VWAP plus/minus tolerance, including shallow penetration) and the alert fires "
                   "when that candle CLOSES green above VWAP, with the candle before it also above VWAP.",
                   "long", _tf_options(1, 3, 5, 15), params=_VWAP_TOUCH,
                   sessions=("rth",), default_options=("3",)))
    add(TriggerDef("vwap_resistance", "VWAP resistance hold", "Crosses & levels",
                   "A candle rallies into a bounded VWAP touch band (VWAP plus/minus tolerance, including shallow penetration) and the alert fires "
                   "when that candle CLOSES red below VWAP, with the candle before it also below VWAP.",
                   "short", _tf_options(1, 3, 5, 15), params=_VWAP_TOUCH,
                   sessions=("rth",), default_options=("3",)))
    add(TriggerDef("back_to_ema", "Back to EMA", "Crosses & levels",
                   "Each of N completed candles stayed the configured distance from its own EMA-at-close; the current bar then touches the current EMA and closes on the original side (from above = long pullback, from below = short).",
                   "both", (OptionDef("ema9_1", "EMA(9) 1 min"), OptionDef("ema20_1", "EMA(20) 1 min"),
                            OptionDef("ema9_5", "EMA(9) 5 min"), OptionDef("ema20_5", "EMA(20) 5 min"),
                            OptionDef("ema9_15", "EMA(9) 15 min"), OptionDef("ema21_15", "EMA(21) 15 min")), "EMA",
                   params=(ParamDef("away_pct", "Min distance while away", 0.5, 0.05, 10, 0.05, "%"),
                           ParamDef("away_candles", "Candles away", 5, 2, 50, 1, "candles")),
                   default_options=("ema9_5",)))
    add(TriggerDef("running", "Momentum run up / down", "Crosses & levels",
                   "The stock moved at least X in the last 60 seconds (one 1-min bar, close to close). "
                   "Measured in picks what X is: a percent of price, a share of the daily ATR, or "
                   "a multiple of the stock's own recent 1-minute candles.",
                   "both", _UPDOWN, "Direction",
                   # min_pct keeps its key so saved setups load unchanged; since
                   # width_unit it holds a percent, an ATR multiple or a candle multiple.
                   params=(ParamDef("min_pct", "Min change", 0.5, 0.01, 20, 0.01, "",
                                    "How far the 1-minute close moved, in the unit chosen next to it."),
                           _move_unit("% of price is the same number for every stock, so 0.5% is a "
                                      "burst on a slow stock and an ordinary minute on a fast one. "
                                      "x daily ATR scales it to how much the stock moves in a day. "
                                      "x avg candle compares the move with the stock's last 20 "
                                      "one-minute candles, so it adjusts to a busy open and a quiet lunch.")),
                   default_options=("up",)))
    add(TriggerDef("pct_change", "Percent change from close", "Crosses & levels",
                   "Price crosses +X% (up) or -X% (down) versus yesterday's close. Once per crossing.",
                   "both", _UPDOWN, "Direction", params=(ParamDef("pct", "Threshold", 5.0, 0.5, 50, 0.5, "%"),), default_options=("up",)))
    add(TriggerDef("gap", "Gap at the open", "Crosses & levels",
                   "First regular-session bar gaps at least X% up (long) or down (short) from yesterday's close. Once per day.",
                   "both", _UPDOWN, "Direction", params=(ParamDef("min_pct", "Min gap", 2.0, 0.2, 50, 0.1, "%"),),
                   sessions=("rth",), default_options=("up",)))
    add(TriggerDef("rvol_cross", "Relative volume crosses", "Crosses & levels",
                   "Time-of-day RVOL (session volume vs the 20-day profile) crosses above X. Once per crossing.",
                   "neutral", params=(ParamDef("threshold", "RVOL", 2.0, 0.5, 20, 0.1, "x"),),
                   sessions=("rth",)))
    add(TriggerDef("rs_spy", "Relative strength vs SPY (15 min)", "Crosses & levels",
                   "The stock's 15-min move minus SPY's crosses above +X% (strong) or below -X% (weak). Once per crossing.",
                   "both", (OptionDef("strong", "RS (stronger than SPY)", "long"), OptionDef("weak", "RW (weaker than SPY)", "short")),
                   "Side", params=(ParamDef("pct", "Threshold", 0.5, 0.1, 10, 0.1, "%"),),
                   sessions=("rth",), default_options=("strong",)))
    add(TriggerDef("momentum_burst", "Momentum burst", "Crosses & levels",
                   "A single 1-min bar with range at least X times the daily ATR, closing in the top (long) or bottom (short) of its range.",
                   "both", _UPDOWN, "Direction", params=(ParamDef("range_mult", "Range / daily ATR", 0.25, 0.05, 2, 0.05, "x"),
                                                        ParamDef("close_pct", "Close in range", 70, 50, 100, 5, "%")),
                   sessions=("rth",), default_options=("up",)))

    for study_id, label, _study, _column, default in _CLASSIC_TRIGGER_SPECS:
        add(TriggerDef(
            f"ta_{study_id}_cross", f"{label} crosses level", "Classic indicators",
            f"On a completed candle, Pandas TA Classic {label} crosses the chosen level. "
            "Re-arms only after crossing back. Warm-up candles cannot trigger.",
            "both", _UPDOWN, "Direction",
            params=(ParamDef("tf", "Timeframe", 5, 1, 60, 1, "min",
                             choices=(1.0, 2.0, 5.0, 15.0, 30.0, 60.0)),
                    ParamDef("level", "Crossing level", default, -1_000_000_000,
                             1_000_000_000, 0.1)),
            sessions=("rth",), default_options=("up",),
        ))

    # ── built-in system setups, passed through ───────────────────────────
    from scanner import plugins
    for sys_setup in plugins.SYSTEM_SETUPS:
        code, name, d = sys_setup.code, sys_setup.name, sys_setup.direction
        add(TriggerDef(f"setup:{code}", name, "System setups",
                       "Fires when the built-in system setup emits this alert (all of its gates apply). "
                       "Use it to extend a system setup with extra triggers without changing the setup itself.",
                       d, sessions=("rth",), source="system"))
    return cat


_CLASSIC_TRIGGER_SPECS = (
    ("macd_hist", "MACD histogram", "macd", "histogram", 0.0),
    ("rsi", "RSI", "rsi", "value", 50.0),
    ("stoch_k", "Stochastic %K", "stoch", "k", 80.0),
    ("cci", "CCI", "cci", "value", 100.0),
    ("bb_percent_b", "Bollinger %B", "bbands", "percent_b", 1.0),
    ("adx", "ADX", "adx", "value", 25.0),
)


EVENT_LIFETIMES: dict[str, str] = {
    "hod": "new_extreme", "hod_ext": "new_extreme", "near_hod": "approach_edge",
    "hi_lo_60d": "first_breach_per_day", "hi_lo_52w": "first_breach_per_day",
    "prior_day_break": "first_breach_per_day", "pm_break": "first_breach_per_day",
    "new_candle_high": "once_per_candle", "new_candle_low": "once_per_candle",
    "break_recent_high": "once_per_candle", "break_recent_low": "once_per_candle",
    "near_last_high": "approach_edge", "near_last_low": "approach_edge",
    "reject_last_high": "completed_candle", "reject_last_low": "completed_candle",
    "orb_breakout": "once_per_day", "orb_breakdown": "once_per_day",
    "orb_trade_cross": "once_per_day",
    "bull_candle_close": "completed_candle", "bear_candle_close": "completed_candle",
    "bull_engulfing": "completed_candle", "bear_engulfing": "completed_candle",
    "bull_harami": "completed_candle", "bear_harami": "completed_candle",
    "doji": "completed_candle", "inside_bar": "completed_candle",
    "double_inside_bar": "completed_candle", "upper_shadow": "completed_candle",
    "lower_shadow": "completed_candle", "volume_spike": "completed_candle",
    "consec_candles": "streak_edge", "cross_above": "recross",
    "cross_below": "recross", "vwap_v": "completed_candle",
    "range_break": "range_exit_edge", "ema_cross_ema": "recross",
    "through_vwap": "recross", "vwap_support": "completed_candle",
    "vwap_resistance": "completed_candle", "back_to_ema": "once_per_candle",
    "running": "qualifying_bar", "pct_change": "recross",
    "gap": "once_per_day", "rvol_cross": "recross",
    "rs_spy": "recross", "momentum_burst": "qualifying_bar",
}
EVENT_LIFETIMES.update({f"ta_{sid}_cross": "completed_candle_recross"
                        for sid, *_ in _CLASSIC_TRIGGER_SPECS})


CATALOG: list[TriggerDef] = [
    replace(trigger, lifetime=EVENT_LIFETIMES[trigger.id] if trigger.source == "native" else "upstream_setup")
    for trigger in _build_catalog()
]
BY_ID: dict[str, TriggerDef] = {t.id: t for t in CATALOG}


def catalog_json() -> list[dict]:
    return [t.to_json() for t in CATALOG]


# ── per-symbol bar series ────────────────────────────────────────────────────

def _session_of(et_min: int) -> str:
    if et_min < _RTH_OPEN:
        return "pre"
    if et_min < _RTH_CLOSE:
        return "rth"
    return "post"


def candle_key(et_min: int, tf: int) -> tuple[str, int]:
    """Clock-aligned candle id anchored at 04:00 (premarket) or 09:30 (RTH)."""
    s = _session_of(et_min)
    if s == "pre":
        return ("pre", (et_min - _PRE_OPEN) // tf)
    return ("rth", (et_min - _RTH_OPEN) // tf)


def et_minutes(ts) -> int:
    t = pd.Timestamp(ts)
    if t.tzinfo is None:
        t = t.tz_localize("UTC")
    t = t.tz_convert("America/New_York")
    return t.hour * 60 + t.minute


class _Ema(SeededEMA):
    """Compatibility name for the shared completed-candle EMA implementation."""


class SymbolSeries:
    """Candles per timeframe for one symbol, plus EMAs and daily levels."""

    def __init__(self, symbol: str) -> None:
        self.symbol = symbol
        self.m1: deque[dict] = deque(maxlen=_M1_RING)
        self.candles: dict[int, deque[dict]] = {tf: deque(maxlen=_CANDLE_RING) for tf in TIMEFRAMES}
        self.partial: dict[int, Optional[dict]] = {tf: None for tf in TIMEFRAMES}
        self.completed: dict[int, bool] = {tf: False for tf in TIMEFRAMES}   # a candle completed on this bar
        self.emas: dict[tuple[int, int], _Ema] = {}
        self._studies: dict[tuple[int, str, int], tuple[tuple, pd.DataFrame]] = {}
        self.daily: dict[str, Optional[float]] = {"ema9_d": None, "ema21_d": None, "ema50_d": None,
                                                  "hi_52w": None, "lo_52w": None, "days": 0}
        # completed sessions' highs and lows, oldest first, for N-day levels
        self.daily_highs: list[float] = []
        self.daily_lows: list[float] = []
        self.day_high: Optional[float] = None     # RTH
        self.day_low: Optional[float] = None
        self.ext_high: Optional[float] = None     # pre + RTH
        self.ext_low: Optional[float] = None
        self.prev_day_high: Optional[float] = None
        self.prev_day_low: Optional[float] = None
        self.prev_ext_high: Optional[float] = None
        self.prev_ext_low: Optional[float] = None
        self.session_date: Optional[str] = None
        self.last_vwap: Optional[float] = None
        self.mem: dict[str, Any] = {}             # trigger scratch state (edges, once-per-day flags)

    # ── seeding ──
    def ema(self, tf: int, period: int) -> _Ema:
        e = self.emas.get((tf, period))
        if e is None:
            e = self.emas[(tf, period)] = _Ema(period)
            # seed from candles already known for that timeframe
            candles = [c for c in self.candles[tf] if c.get("session", "rth") == "rth"]
            for c, value in zip(candles, e.seed([c["close"] for c in candles])):
                c.setdefault("ema_at_close", {})[period] = None if pd.isna(value) else float(value)
        return e

    def want_ema(self, tf: int, period: int) -> None:
        self.ema(tf, period)

    def study(self, tf: int, name: str, length: int | None = None) -> pd.DataFrame:
        """Classic study over completed candles, cached until that ring changes."""
        cs = self.candles.get(tf)
        if cs is None:
            raise ValueError(f"unsupported timeframe: {tf}")
        last = cs[-1] if cs else None
        stamp = (len(cs), last.get("key") if last else None,
                 last.get("close") if last else None)
        key = (tf, name, int(length or 0))
        cached = self._studies.get(key)
        if cached is None or cached[0] != stamp:
            result = calculate(candles_frame(list(cs)), name, length)
            self._studies[key] = (stamp, result)
            return result
        return cached[1]

    def seed_daily(self, daily: Optional[pd.DataFrame]) -> None:
        if daily is None or daily.empty:
            return
        try:
            closes = [float(x) for x in daily["close"].tolist()]
            highs = [float(x) for x in daily["high"].tolist()]
            lows = [float(x) for x in daily["low"].tolist()]
        except Exception:
            return
        for key, period in (("ema9_d", 9), ("ema21_d", 21), ("ema50_d", 50)):
            e = _Ema(period)
            e.seed(closes)
            self.daily[key] = e.value
        tail_h, tail_l = highs[-252:], lows[-252:]
        self.daily_highs, self.daily_lows = tail_h, tail_l
        self.daily["hi_52w"] = max(tail_h) if tail_h else None
        self.daily["lo_52w"] = min(tail_l) if tail_l else None
        self.daily["days"] = len(tail_h)

    def seed_intraday(self, bars_5m: Optional[pd.DataFrame]) -> None:
        """Seed the 5/15/30/60-min candle rings from 5-min history so the intraday
        EMAs have a past. 1-, 2- and 3-min candles are session-only."""
        if bars_5m is None or bars_5m.empty:
            return
        try:
            idx = bars_5m.index
            if getattr(idx, "tz", None) is None:
                idx = idx.tz_localize("UTC")
            et = idx.tz_convert("America/New_York")
            rows = zip(et, bars_5m["open"].tolist(), bars_5m["high"].tolist(),
                       bars_5m["low"].tolist(), bars_5m["close"].tolist(), bars_5m["volume"].tolist())
        except Exception:
            return
        for t, o, h, l, c, v in rows:
            em = t.hour * 60 + t.minute
            if _session_of(em) != "rth":
                continue
            day = t.strftime("%Y-%m-%d")
            for tf in (5, 15, 30, 60):
                key = (day,) + candle_key(em, tf)
                p = self.partial[tf]
                if p is None or p["key"] != key:
                    if p is not None:
                        self.candles[tf].append(p)
                    self.partial[tf] = {"key": key, "open": float(o), "high": float(h), "low": float(l),
                                        "close": float(c), "volume": float(v), "vwap": None, "et_min": em}
                else:
                    p["high"] = max(p["high"], float(h)); p["low"] = min(p["low"], float(l))
                    p["close"] = float(c); p["volume"] += float(v)
        # close whatever is still open (history ends at yesterday's close)
        for tf in (5, 15, 30, 60):
            p = self.partial[tf]
            if p is not None:
                self.candles[tf].append(p)
                self.partial[tf] = None
        for (tf, period), e in list(self.emas.items()):
            tracker = self.emas[(tf, period)] = _Ema(period)
            candles = [c for c in self.candles[tf] if c.get("session", "rth") == "rth"]
            for c, value in zip(candles, tracker.seed([c["close"] for c in candles])):
                c.setdefault("ema_at_close", {})[period] = None if pd.isna(value) else float(value)

    # ── live ──
    def on_bar(self, bar: dict, et_min: int, day: str, vwap: Optional[float]) -> str:
        """Append a 1-min bar. Returns the session tag."""
        if day != self.session_date:
            # Close yesterday's unfinished higher-timeframe candles into
            # history without presenting them as *today's* completion event.
            for tf in (5, 15, 30, 60):
                prior = self.partial[tf]
                if prior is not None:
                    self.candles[tf].append(prior)
                    for (etf, period), tracker in self.emas.items():
                        if etf == tf and prior["session"] == "rth":
                            prior.setdefault("ema_at_close", {})[period] = tracker.push(prior["close"])
                    self.partial[tf] = None
            # Carry the completed live session into the rolling daily reference.
            # The initial startup transition has no day extrema, so it cannot
            # duplicate the history loaded by seed_daily().
            if self.session_date is not None and self.day_high is not None and self.day_low is not None:
                self.daily_highs = (self.daily_highs + [self.day_high])[-252:]
                self.daily_lows = (self.daily_lows + [self.day_low])[-252:]
                self.daily["hi_52w"] = max(self.daily_highs) if self.daily_highs else None
                self.daily["lo_52w"] = min(self.daily_lows) if self.daily_lows else None
                self.daily["days"] = len(self.daily_highs)
            self.session_date = day
            self.day_high = self.day_low = None
            self.ext_high = self.ext_low = None
            self.m1.clear()
            for tf in (1, 2, 3):
                self.candles[tf].clear()
                self.partial[tf] = None
                for (etf, period) in list(self.emas):
                    if etf == tf:
                        self.emas[(etf, period)] = _Ema(period)
            self.mem = {}
        sess = _session_of(et_min)
        o, h, l, c, v = float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"]), float(bar["volume"])
        self.m1.append({"et_min": et_min, "session": sess, "open": o, "high": h, "low": l, "close": c,
                        "volume": v, "vwap": vwap})
        self.last_vwap = vwap
        self.prev_ext_high, self.prev_ext_low = self.ext_high, self.ext_low
        self.ext_high = h if self.ext_high is None else max(self.ext_high, h)
        self.ext_low = l if self.ext_low is None else min(self.ext_low, l)
        self.prev_day_high, self.prev_day_low = self.day_high, self.day_low
        if sess == "rth":
            self.day_high = h if self.day_high is None else max(self.day_high, h)
            self.day_low = l if self.day_low is None else min(self.day_low, l)
        for tf in TIMEFRAMES:
            self.completed[tf] = False
            key = (day,) + candle_key(et_min, tf)
            p = self.partial[tf]
            if p is None or p["key"] != key:
                if p is not None:
                    self.candles[tf].append(p)
                    self.completed[tf] = True
                    for (etf, period), e in self.emas.items():
                        if etf == tf and p["session"] == "rth":
                            p.setdefault("ema_at_close", {})[period] = e.push(p["close"])
                self.partial[tf] = {"key": key, "open": o, "high": h, "low": l, "close": c, "volume": v,
                                    "vwap": vwap, "et_min": et_min, "session": sess}
            else:
                p["high"] = max(p["high"], h); p["low"] = min(p["low"], l)
                p["close"] = c; p["volume"] += v; p["vwap"] = vwap
        return sess

    # ── helpers for triggers ──
    def last_completed(self, tf: int, n: int = 1) -> list[dict]:
        d = self.candles[tf]
        if len(d) < n:
            return []
        return list(d)[-n:]

    def atr(self, tf: int, n: int = 20) -> Optional[float]:
        values = self.study(tf, "atr", n)
        return _f(values["value"].iloc[-1]) if not values.empty else None

    def swing(self, tf: int, lookback: int, side: str) -> Optional[float]:
        cs = self.last_completed(tf, lookback)
        if not cs:
            return None
        return max(c["high"] for c in cs) if side == "high" else min(c["low"] for c in cs)

    def prev_close_1m(self) -> Optional[float]:
        if len(self.m1) < 2:
            return None
        return self.m1[-2]["close"]

    def session_open(self) -> Optional[float]:
        for b in self.m1:
            if b["session"] == "rth":
                return b["open"]
        return None

    def pm_levels(self) -> tuple[Optional[float], Optional[float]]:
        hs = [b["high"] for b in self.m1 if b["session"] == "pre"]
        ls = [b["low"] for b in self.m1 if b["session"] == "pre"]
        return (max(hs) if hs else None, min(ls) if ls else None)


# ── level helpers ────────────────────────────────────────────────────────────
#
# Shared by the triggers below and by the universe conditions in
# scanner/conditions.py. The distinction matters:
#
#   a TRIGGER has EDGE semantics: it fires once, on the transition, and
#       latches its state in `series.mem`
#   a CONDITION has LEVEL semantics: "is this true right now", asked on every
#       bar, with no latching and no memory
#
# Calling a trigger's `_impl` from a condition would corrupt that latch and
# silently change when the trigger fires. So the arithmetic lives here, and the
# trigger wraps it in `edge()` while the condition calls it raw.

def candle_rel_volume(series: "SymbolSeries", tf: int, lookback: int) -> Optional[float]:
    """Latest COMPLETED tf-candle's volume over the mean of the prior `lookback`.

    Only meaningful on the bar that completes a candle; otherwise the value is
    stale by up to tf-1 minutes. `rolling_rel_volume` is the every-bar version.
    """
    cs = series.last_completed(tf, lookback + 1)
    if len(cs) < lookback + 1:
        return None
    avg = sum(x["volume"] for x in cs[:-1]) / lookback
    if avg <= 0:
        return None
    return cs[-1]["volume"] / avg


def rolling_rel_volume(series: "SymbolSeries", tf: int, lookback: int,
                       session: Optional[str] = None) -> Optional[float]:
    """Volume of the trailing `tf` minutes over the mean of the `lookback`
    preceding non-overlapping tf-minute blocks. Updates every bar.

    Filters to `session` before summing. That is not optional: `m1` holds
    premarket bars tagged "pre" and is only cleared on a DAY change, so an
    unfiltered baseline at 09:35 would average thin premarket volume and make
    the ratio explode on exactly the gappers this is meant to vet.
    """
    if tf < 1 or lookback < 1:
        return None
    bars = [b for b in series.m1 if session is None or b["session"] == session]
    need = tf * (lookback + 1)
    if len(bars) < need:
        return None
    window = bars[-need:]
    current = sum(b["volume"] for b in window[-tf:])
    blocks = [window[i * tf:(i + 1) * tf] for i in range(lookback)]
    prior = [sum(b["volume"] for b in blk) for blk in blocks]
    avg = sum(prior) / len(prior)
    if avg <= 0:
        return None
    return current / avg


def consec_streak(series: "SymbolSeries", tf: int,
                  session: Optional[str] = None) -> Optional[int]:
    """Signed run length over COMPLETED tf-candles.

    +N for N consecutive closes above their open, -N for N consecutive below,
    0 when the most recent candle is a doji, None when there is no history.
    """
    if tf not in TIMEFRAMES:
        return None
    cs = list(series.candles[tf])
    if session is not None:
        cs = [
            candle for candle in cs
            if candle["key"][0] == series.session_date and candle["key"][1] == session
        ]
    if not cs:
        return None
    last = cs[-1]
    if last["close"] > last["open"]:
        sign, test = 1, (lambda x: x["close"] > x["open"])
    elif last["close"] < last["open"]:
        sign, test = -1, (lambda x: x["close"] < x["open"])
    else:
        return 0
    n = 0
    for x in reversed(cs):
        if not test(x):
            break
        n += 1
    return sign * n


# ── evaluation context ───────────────────────────────────────────────────────

@dataclass
class Fire:
    direction: str
    value: Optional[float] = None
    note: str = ""


def level_value(s, st, key: str) -> Optional[float]:
    """One of LEVELS as a price.

    Module-level so `scanner.conditions` can ask the same question of a
    ConditionCtx: one resolver means a trigger and a condition can never
    disagree about where a level is. `s` (SymbolSeries) may be None, in which
    case only the levels SymbolState carries resolve.
    """
    if key == "open":
        return s.session_open() if s is not None else _f(getattr(st, "session_open", None))
    if key == "prior_close":
        return _f(getattr(st, "prior_close", None))
    if key == "vwap":
        return _f(getattr(st, "vwap", None))
    if key in ("pm_high", "pm_low"):
        i = 0 if key == "pm_high" else 1
        v = s.pm_levels()[i] if s is not None else None
        return v if v is not None else _f(getattr(st, key, None))
    if key == "prior_high":
        return _f(getattr(st, "prior_high", None))
    if key == "prior_low":
        return _f(getattr(st, "prior_low", None))
    if key == "sma50_d":
        return _f(getattr(st, "sma_50", None))
    if key == "sma100_d":
        return _f(getattr(st, "sma_100", None))
    if key == "sma200_d":
        return _f(getattr(st, "sma_200", None))
    if s is None:
        return None
    if key in ("ema9_d", "ema21_d", "ema50_d"):
        return s.daily.get(key)
    if key.startswith("ema"):
        period, tf = key[3:].split("_")
        return s.ema(int(tf), int(period)).value
    return None


@dataclass
class EvalCtx:
    state: Any                     # SymbolState
    series: SymbolSeries
    bar: dict
    et_min: int
    session: str
    external: set[str]             # "setup:<code>"
    spy_mom_15m: Optional[float] = None
    resolved_levels: dict = field(default_factory=dict)
    memory_scope: str = ""

    @property
    def close(self) -> float:
        return float(self.bar["close"])

    @property
    def prev_close(self) -> Optional[float]:
        return self.series.prev_close_1m()

    def level(self, key: str) -> Optional[float]:
        if key in self.resolved_levels:
            return self.resolved_levels[key]
        v = self._level(key)
        self.resolved_levels[key] = v
        return v

    def _level(self, key: str) -> Optional[float]:
        return level_value(self.series, self.state, key)

    def once(self, key: str) -> bool:
        """True the first time `key` is seen today (per symbol)."""
        key = f"{self.memory_scope}|{key}" if self.memory_scope else key
        if self.series.mem.get(key):
            return False
        self.series.mem[key] = True
        return True

    def edge(self, key: str, cond: bool) -> bool:
        """True when `cond` turns True (was False or unknown before)."""
        key = f"{self.memory_scope}|{key}" if self.memory_scope else key
        was = self.series.mem.get(key, False)
        self.series.mem[key] = cond
        return cond and not was


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


# ── trigger implementations ──────────────────────────────────────────────────
# Each returns Fire or None. `opt` is the selected option key ("" when the
# trigger has no options). `p` holds resolved params (defaults filled in).

TriggerFn = Callable[[EvalCtx, str, dict], Optional[Fire]]
_IMPL: dict[str, TriggerFn] = {}


def _impl(tid: str):
    def deco(fn: TriggerFn) -> TriggerFn:
        _IMPL[tid] = fn
        return fn
    return deco


def _cross(prev: Optional[float], cur: float, level: Optional[float], up: bool) -> bool:
    if prev is None or level is None:
        return False
    return (prev <= level < cur) if up else (prev >= level > cur)


@_impl("hod")
def _t_hod(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    s = c.series
    if opt == "high":
        if s.prev_day_high is not None and float(c.bar["high"]) > s.prev_day_high:
            return Fire("long", float(c.bar["high"]), "new high of day")
    else:
        if s.prev_day_low is not None and float(c.bar["low"]) < s.prev_day_low:
            return Fire("short", float(c.bar["low"]), "new low of day")
    return None


@_impl("hod_ext")
def _t_hod_ext(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    s = c.series
    if opt == "high":
        if s.prev_ext_high is not None and float(c.bar["high"]) > s.prev_ext_high:
            return Fire("long", float(c.bar["high"]), "new high incl. premarket")
    else:
        if s.prev_ext_low is not None and float(c.bar["low"]) < s.prev_ext_low:
            return Fire("short", float(c.bar["low"]), "new low incl. premarket")
    return None


@_impl("near_hod")
def _t_near_hod(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    s = c.series
    atr = s.atr(1, 20)
    if atr is None or atr <= 0:
        return None
    if opt == "high":
        lvl = s.prev_day_high
        if lvl is None:
            return None
        dist = lvl - c.close
        near = 0 < dist < atr and float(c.bar["high"]) <= lvl
        if c.edge(f"near_hod:{opt}", near):
            return Fire("long", dist, f"{dist:.2f} below HOD {lvl:.2f}")
    else:
        lvl = s.prev_day_low
        if lvl is None:
            return None
        dist = c.close - lvl
        near = 0 < dist < atr and float(c.bar["low"]) >= lvl
        if c.edge(f"near_hod:{opt}", near):
            return Fire("short", dist, f"{dist:.2f} above LOD {lvl:.2f}")
    return None


def n_day_level(series: "SymbolSeries", state: Any, days: int, side: str) -> Optional[float]:
    """Highest high / lowest low of the last `days` completed sessions, or None
    when fewer than `days` are loaded. Shared by the trigger and the Stock check.

    Reads the series' daily lists, which hold up to 252 sessions. Falls back to the
    state's 60-session window only when the series was never seeded (a state built
    without CustomEvaluator warmup), and only when that window is long enough.
    """
    vals = series.daily_highs if side == "high" else series.daily_lows
    if not vals:
        try:
            s = state.daily_highs_60d if side == "high" else state.daily_lows_60d
            vals = [float(x) for x in s] if s is not None else []
        except Exception:
            vals = []
    if days < 1 or len(vals) < days:
        return None
    window = vals[-days:]
    return max(window) if side == "high" else min(window)


def n_day_latch_key(days: int, side: str) -> str:
    """Session-scoped memory key for an N-day high/low side."""
    return f"hi_lo_60d:{int(days)}:{side}:alerted"


def milestone_latch_key(trigger_id: str, side: str) -> str:
    return f"{trigger_id}:{side}:alerted"


def _first_milestone_breach(c: EvalCtx, trigger_id: str, side: str,
                            level: Optional[float], prior_extreme: Optional[float],
                            note: str) -> Optional[Fire]:
    if level is None:
        return None
    latch = milestone_latch_key(trigger_id, side)
    if c.series.mem.get(latch):
        return None
    high = side == "high"
    if prior_extreme is not None and (prior_extreme > level if high else prior_extreme < level):
        c.series.mem[latch] = True  # breached in replay before this trigger was evaluated
        return None
    extreme = float(c.bar["high" if high else "low"])
    breached = extreme > level if high else extreme < level
    if breached:
        c.series.mem[latch] = True
        return Fire("long" if high else "short", level, note)
    return None


@_impl("hi_lo_60d")
def _t_60d(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    days = int(p.get("days", 60) or 60)
    lvl = n_day_level(c.series, c.state, days, opt)
    if lvl is None:
        return None
    latch = n_day_latch_key(days, opt)
    if c.series.mem.get(latch):
        return None

    # seed_session_bar() intentionally advances the series without evaluating
    # alerts. If those seeded bars already breached the level, latch silently so
    # a restart cannot emit a stale alert on the next live HOD/LOD.
    prior_extreme = c.series.prev_day_high if opt == "high" else c.series.prev_day_low
    already_breached = (prior_extreme is not None and
                        (prior_extreme > lvl if opt == "high" else prior_extreme < lvl))
    if already_breached:
        c.series.mem[latch] = True
        return None

    extreme = float(c.bar["high" if opt == "high" else "low"])
    breached = extreme > lvl if opt == "high" else extreme < lvl
    if breached:
        c.series.mem[latch] = True
        return Fire("long" if opt == "high" else "short", lvl,
                    f"first {days}-day {opt} beyond {lvl:.2f}")
    return None


@_impl("hi_lo_52w")
def _t_52w(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    lvl = c.series.daily.get("hi_52w" if opt == "high" else "lo_52w")
    prior = c.series.prev_day_high if opt == "high" else c.series.prev_day_low
    return _first_milestone_breach(c, "hi_lo_52w", opt, lvl, prior,
                                   f"first {c.series.daily.get('days')}-day {opt} beyond {lvl:.2f}" if lvl is not None else "")


@_impl("prior_day_break")
def _t_pd(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    lvl = c.level("prior_high" if opt == "high" else "prior_low")
    prior = c.series.prev_ext_high if opt == "high" else c.series.prev_ext_low
    return _first_milestone_breach(c, "prior_day_break", opt, lvl, prior,
                                   f"first prior day {opt} beyond {lvl:.2f}" if lvl is not None else "")


@_impl("pm_break")
def _t_pm(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    lvl = c.level("pm_high" if opt == "high" else "pm_low")
    prior = c.series.prev_day_high if opt == "high" else c.series.prev_day_low
    return _first_milestone_breach(c, "pm_break", opt, lvl, prior,
                                   f"first premarket {opt} beyond {lvl:.2f}" if lvl is not None else "")


def _new_candle_extreme(c: EvalCtx, tf: int, since: int, side: str) -> Optional[Fire]:
    s = c.series
    cur = s.partial[tf]
    prev = s.last_completed(tf, since)
    if cur is None or len(prev) < since:
        return None
    key = f"nc{side}:{tf}:{cur['key']}"
    if side == "high":
        lvl = max(x["high"] for x in prev)
        if float(c.bar["high"]) > lvl and c.once(key):
            return Fire("long", lvl, f"above {TF_LABEL[tf]} high {lvl:.2f}")
    else:
        lvl = min(x["low"] for x in prev)
        if float(c.bar["low"]) < lvl and c.once(key):
            return Fire("short", lvl, f"below {TF_LABEL[tf]} low {lvl:.2f}")
    return None


@_impl("new_candle_high")
def _t_nch(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    return _new_candle_extreme(c, int(opt), int(p["since"]), "high")


@_impl("new_candle_low")
def _t_ncl(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    return _new_candle_extreme(c, int(opt), int(p["since"]), "low")


def _break_recent(c: EvalCtx, tf: int, lookback: int, side: str) -> Optional[Fire]:
    s = c.series
    lvl = s.swing(tf, lookback, side)
    cur = s.partial[tf]
    if lvl is None or cur is None:
        return None
    if _cross(c.prev_close, c.close, lvl, side == "high") and c.once(f"br{side}:{tf}:{cur['key']}"):
        return Fire("long" if side == "high" else "short", lvl, f"{TF_LABEL[tf]} swing {side} {lvl:.2f}")
    return None


@_impl("break_recent_high")
def _t_brh(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    return _break_recent(c, int(opt), int(p["lookback"]), "high")


@_impl("break_recent_low")
def _t_brl(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    return _break_recent(c, int(opt), int(p["lookback"]), "low")


def _near_last(c: EvalCtx, tf: int, lookback: int, side: str) -> Optional[Fire]:
    s = c.series
    lvl = s.swing(tf, lookback, side)
    atr = s.atr(tf, 20)
    if lvl is None or atr is None or atr <= 0:
        return None
    dist = (lvl - c.close) if side == "high" else (c.close - lvl)
    inside = float(c.bar["high"]) <= lvl if side == "high" else float(c.bar["low"]) >= lvl
    near = 0 < dist < atr and inside
    if c.edge(f"near:{side}:{tf}", near):
        return Fire("long" if side == "high" else "short", dist, f"{dist:.2f} from {TF_LABEL[tf]} {side} {lvl:.2f}")
    return None


@_impl("near_last_high")
def _t_nlh(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    return _near_last(c, int(opt), int(p["lookback"]), "high")


@_impl("near_last_low")
def _t_nll(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    return _near_last(c, int(opt), int(p["lookback"]), "low")


def _reject(c: EvalCtx, tf: int, lookback: int, side: str) -> Optional[Fire]:
    s = c.series
    if not s.completed[tf]:
        return None
    cs = s.last_completed(tf, lookback + 1)
    if len(cs) < 2:
        return None
    cur, prev = cs[-1], cs[:-1]
    if side == "high":
        lvl = max(x["high"] for x in prev)
        if cur["high"] >= lvl and cur["close"] < lvl and cur["close"] < cur["open"]:
            return Fire("short", lvl, f"rejected {TF_LABEL[tf]} high {lvl:.2f}")
    else:
        lvl = min(x["low"] for x in prev)
        if cur["low"] <= lvl and cur["close"] > lvl and cur["close"] > cur["open"]:
            return Fire("long", lvl, f"rejected {TF_LABEL[tf]} low {lvl:.2f}")
    return None


@_impl("reject_last_high")
def _t_rlh(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    return _reject(c, int(opt), int(p["lookback"]), "high")


@_impl("reject_last_low")
def _t_rll(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    return _reject(c, int(opt), int(p["lookback"]), "low")


def _orb(c: EvalCtx, tf: int, up: bool) -> Optional[Fire]:
    s = c.series
    if c.session != "rth":
        return None
    first = None
    # Today's opening candle only. The 5/15/30/60-min rings keep earlier sessions
    # (seeded history, or yesterday in a process left running), so the date in
    # key[0] has to match: the oldest opening candle in the ring is not today's.
    for x in reversed(s.candles[tf]):
        if x["key"][0] != s.session_date:
            break
        if x["key"][1] == "rth" and x["key"][2] == 0:
            first = x
            break
    if first is None:
        return None
    cur = s.partial[tf]
    if cur is not None and cur["key"][2] == 0:
        return None          # still inside the opening candle
    lvl = first["high"] if up else first["low"]
    if _cross(c.prev_close, c.close, lvl, up) and c.once(f"orb:{tf}:{'up' if up else 'dn'}"):
        return Fire("long" if up else "short", lvl, f"{TF_LABEL[tf]} opening range {'high' if up else 'low'} {lvl:.2f}")
    return None


@_impl("orb_breakout")
def _t_orb_up(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    return _orb(c, int(opt), True)


@_impl("orb_breakdown")
def _t_orb_dn(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    return _orb(c, int(opt), False)


# ── candles ──

def _session_completed(c: EvalCtx, tf: int, n: Optional[int] = None) -> list[dict]:
    candles = [
        candle for candle in c.series.candles[tf]
        if candle["key"][0] == c.series.session_date and candle["key"][1] == c.session
    ]
    return candles[-n:] if n is not None else candles


def _completed_pair(c: EvalCtx, tf: int) -> Optional[tuple[dict, dict]]:
    if not c.series.completed[tf]:
        return None
    cs = _session_completed(c, tf, 2)
    if len(cs) < 2:
        return None
    return cs[-2], cs[-1]


def _body(x: dict) -> float:
    return abs(x["close"] - x["open"])


@_impl("bull_candle_close")
def _t_bcc(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    tf = int(opt)
    if not c.series.completed[tf]:
        return None
    x = c.series.last_completed(tf, 1)
    if x and x[0]["close"] > x[0]["open"]:
        return Fire("long", x[0]["close"], f"{TF_LABEL[tf]} green close")
    return None


@_impl("bear_candle_close")
def _t_brc(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    tf = int(opt)
    if not c.series.completed[tf]:
        return None
    x = c.series.last_completed(tf, 1)
    if x and x[0]["close"] < x[0]["open"]:
        return Fire("short", x[0]["close"], f"{TF_LABEL[tf]} red close")
    return None


@_impl("bull_engulfing")
def _t_bull_eng(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    pr = _completed_pair(c, int(opt))
    if not pr:
        return None
    a, b = pr
    if a["close"] < a["open"] and b["close"] > b["open"] and b["open"] <= a["close"] and b["close"] >= a["open"] and _body(b) > _body(a):
        return Fire("long", b["close"], f"{TF_LABEL[int(opt)]} bullish engulfing")
    return None


@_impl("bear_engulfing")
def _t_bear_eng(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    pr = _completed_pair(c, int(opt))
    if not pr:
        return None
    a, b = pr
    if a["close"] > a["open"] and b["close"] < b["open"] and b["open"] >= a["close"] and b["close"] <= a["open"] and _body(b) > _body(a):
        return Fire("short", b["close"], f"{TF_LABEL[int(opt)]} bearish engulfing")
    return None


@_impl("bull_harami")
def _t_bull_har(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    pr = _completed_pair(c, int(opt))
    if not pr:
        return None
    a, b = pr
    if a["close"] < a["open"] and b["close"] > b["open"] and b["open"] >= a["close"] and b["close"] <= a["open"] and _body(b) < _body(a):
        return Fire("long", b["close"], f"{TF_LABEL[int(opt)]} bullish harami")
    return None


@_impl("bear_harami")
def _t_bear_har(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    pr = _completed_pair(c, int(opt))
    if not pr:
        return None
    a, b = pr
    if a["close"] > a["open"] and b["close"] < b["open"] and b["open"] <= a["close"] and b["close"] >= a["open"] and _body(b) < _body(a):
        return Fire("short", b["close"], f"{TF_LABEL[int(opt)]} bearish harami")
    return None


@_impl("doji")
def _t_doji(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    tf = int(opt)
    if not c.series.completed[tf]:
        return None
    x = c.series.last_completed(tf, 1)
    if not x:
        return None
    x = x[0]
    rng = x["high"] - x["low"]
    if rng > 0 and _body(x) / rng * 100 <= float(p["body_pct"]):
        return Fire("neutral", _body(x) / rng * 100, f"{TF_LABEL[tf]} doji")
    return None


@_impl("inside_bar")
def _t_inside(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    pr = _completed_pair(c, int(opt))
    if not pr:
        return None
    a, b = pr
    if b["high"] < a["high"] and b["low"] > a["low"]:
        return Fire("neutral", b["high"] - b["low"], f"{TF_LABEL[int(opt)]} inside bar")
    return None


@_impl("double_inside_bar")
def _t_dinside(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    tf = int(opt)
    if not c.series.completed[tf]:
        return None
    cs = _session_completed(c, tf, 3)
    if len(cs) < 3:
        return None
    a, b, d = cs
    if b["high"] < a["high"] and b["low"] > a["low"] and d["high"] < b["high"] and d["low"] > b["low"]:
        return Fire("neutral", d["high"] - d["low"], f"{TF_LABEL[tf]} double inside bar")
    return None


@_impl("upper_shadow")
def _t_ush(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    tf = int(opt)
    if not c.series.completed[tf]:
        return None
    x = c.series.last_completed(tf, 1)
    if not x:
        return None
    x = x[0]
    body = max(_body(x), 1e-9)
    tail = x["high"] - max(x["open"], x["close"])
    if tail / body >= float(p["ratio"]) and tail > 0:
        return Fire("short", tail / body, f"{TF_LABEL[tf]} upper shadow {tail / body:.1f}x body")
    return None


@_impl("lower_shadow")
def _t_lsh(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    tf = int(opt)
    if not c.series.completed[tf]:
        return None
    x = c.series.last_completed(tf, 1)
    if not x:
        return None
    x = x[0]
    body = max(_body(x), 1e-9)
    tail = min(x["open"], x["close"]) - x["low"]
    if tail / body >= float(p["ratio"]) and tail > 0:
        return Fire("long", tail / body, f"{TF_LABEL[tf]} lower shadow {tail / body:.1f}x body")
    return None


@_impl("volume_spike")
def _t_vspike(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    tf = int(opt)
    if not c.series.completed[tf]:
        return None
    n = int(p["lookback"])
    ratio = candle_rel_volume(c.series, tf, n)      # level math, shared with conditions
    if ratio is None:
        return None
    if ratio >= float(p["ratio"]):
        return Fire("neutral", ratio, f"{TF_LABEL[tf]} volume {ratio:.1f}x avg({n})")
    return None


@_impl("consec_candles")
def _t_consec(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    tf = int(p.get("tf", 5))
    if tf not in TIMEFRAMES or not c.series.completed[tf]:
        return None
    need = int(p["count"])
    if len(c.series.candles[tf]) < need:
        return None
    green = opt == "green"
    # Signed streak, shared with conditions: +N green, -N red. `ok` is exactly
    # the old all()-over-the-last-`need`-candles test.
    streak = consec_streak(c.series, tf, c.session) or 0
    ok = (streak >= need) if green else (streak <= -need)
    # fire once when the streak reaches `need` (the candle before the run must break it, or unknown)
    if ok and c.edge(f"consec:{opt}:{tf}", ok):
        return Fire("long" if green else "short", float(need), f"{need} {opt} {TF_LABEL[tf]} candles")
    if not ok:
        c.series.mem[f"consec:{opt}:{tf}"] = False
    return None


# ── crosses & levels ──

@_impl("cross_above")
def _t_xa(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    if int(p.get("tf", 1) or 1) != 1:
        return _candle_cross(c, opt, int(p["tf"]), True)
    lvl = c.level(opt)
    if _cross(c.prev_close, c.close, lvl, True):
        return Fire("long", lvl, f"crossed above {_level_label(opt)} {lvl:.2f}")
    return None


@_impl("cross_below")
def _t_xb(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    if int(p.get("tf", 1) or 1) != 1:
        return _candle_cross(c, opt, int(p["tf"]), False)
    lvl = c.level(opt)
    if _cross(c.prev_close, c.close, lvl, False):
        return Fire("short", lvl, f"crossed below {_level_label(opt)} {lvl:.2f}")
    return None


def _candle_cross(c: EvalCtx, opt: str, tf: int, up: bool) -> Optional[Fire]:
    """Crossing judged on completed tf-minute candles instead of 1-minute bars.

    Evaluated only on the bar a candle completes, so it fires at most once per
    candle with no latch. The two candles must be from the same day: the first
    candle of the session against yesterday's last one is an overnight gap, not
    a cross anyone saw on a chart.

    VWAP is read off each candle (its value at that candle's close), because
    VWAP moves during a 5- or 15-minute candle and "was below VWAP, now above"
    should compare each close with the VWAP of its own moment. Every other level
    is flat or slow enough within a candle that the current value serves both.
    """
    if tf not in TIMEFRAMES or not c.series.completed[tf]:
        return None
    cs = c.series.last_completed(tf, 2)
    if len(cs) < 2:
        return None
    prev, cur = cs
    if prev["key"][0] != cur["key"][0]:
        return None
    if opt == "vwap":
        lp, lc = prev.get("vwap"), cur.get("vwap")
        if lp is None or lc is None:
            return None
    else:
        lp = lc = c.level(opt)
        if lc is None:
            return None
    label = TF_LABEL.get(tf, f"{tf} Min")
    if up and prev["close"] <= lp and cur["close"] > lc:
        return Fire("long", lc, f"{label} close crossed above {_level_label(opt)} {lc:.2f}")
    if not up and prev["close"] >= lp and cur["close"] < lc:
        return Fire("short", lc, f"{label} close crossed below {_level_label(opt)} {lc:.2f}")
    return None


def _level_label(key: str) -> str:
    for o in LEVELS:
        if o.key == key:
            return o.label
    return key


@_impl("through_vwap")
def _t_through(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    v = c.level("vwap")
    up = opt == "above"
    if not _cross(c.prev_close, c.close, v, up):
        return None
    m1 = [bar for bar in c.series.m1 if bar["session"] == c.session]
    if len(m1) < 11:
        return None
    avg = sum(b["high"] - b["low"] for b in m1[-11:-1]) / 10
    rng = float(c.bar["high"]) - float(c.bar["low"])
    if avg > 0 and rng >= float(p["mult"]) * avg:
        return Fire("long" if up else "short", rng / avg, f"through VWAP {v:.2f} with {rng / avg:.1f}x candle")
    return None


def _vwap_sr(c: EvalCtx, tf: int, tol_pct: float, support: bool, atr_unit: bool = False) -> Optional[Fire]:
    if tf not in TIMEFRAMES or not c.series.completed[tf]:
        return None
    cs = c.series.last_completed(tf, 2)
    if len(cs) < 2:
        return None
    a, b = cs
    v = b.get("vwap")
    va = a.get("vwap")
    if v is None or va is None:
        return None
    if atr_unit:
        atr = _f(getattr(c.state, "atr_d1", None))
        if not atr or atr <= 0:
            return None
        tol = atr * tol_pct / 100.0
    else:
        tol = v * tol_pct / 100.0
    if support:
        if a["close"] > va and v - tol <= b["low"] <= v + tol and b["close"] > v and b["close"] > b["open"]:
            return Fire("long", v, f"{TF_LABEL[tf]} bounce off VWAP {v:.2f}")
    else:
        if a["close"] < va and v - tol <= b["high"] <= v + tol and b["close"] < v and b["close"] < b["open"]:
            return Fire("short", v, f"{TF_LABEL[tf]} rejection at VWAP {v:.2f}")
    return None


@_impl("vwap_support")
def _t_vs(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    return _vwap_sr(c, int(opt), float(p["tol_pct"]), True, int(p.get("tol_unit", 0) or 0) == 1)


@_impl("vwap_resistance")
def _t_vr(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    return _vwap_sr(c, int(opt), float(p["tol_pct"]), False, int(p.get("tol_unit", 0) or 0) == 1)


@_impl("back_to_ema")
def _t_bte(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    period, tf = opt[3:].split("_")
    tf, period = int(tf), int(period)
    s = c.series
    e = s.ema(tf, period)
    if e.value is None:
        return None
    n = int(p["away_candles"])
    away = float(p["away_pct"]) / 100.0
    cs = [x for x in s.candles[tf] if x.get("session", "rth") == "rth"][-n:]
    if len(cs) < n:
        return None
    # Compare each completed candle with the EMA that existed at that close.
    history = [x.get("ema_at_close", {}).get(period) for x in cs]
    if any(value is None for value in history):
        return None
    ev = e.value
    above = all(x["low"] > at * (1 + away) for x, at in zip(cs, history))
    below = all(x["high"] < at * (1 - away) for x, at in zip(cs, history))
    cur = s.partial[tf]
    if cur is None:
        return None
    key = f"bte:{opt}:{cur['key']}"
    if above and float(c.bar["low"]) <= ev <= c.close and c.once(key):
        return Fire("long", ev, f"back to {_level_label(opt) if opt in [o.key for o in LEVELS] else opt} {ev:.2f} from above")
    if below and float(c.bar["high"]) >= ev >= c.close and c.once(key):
        return Fire("short", ev, f"back to {opt} {ev:.2f} from below")
    return None


@_impl("vwap_v")
def _t_vwap_v(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    """A V into VWAP and back out, judged on the candle that just completed.

    Three parts, each aimed at the difference between a bounce and a stock that
    is merely parked on VWAP:

      came from distance  some candle in the window closed at least `away_atr`
                          daily ATRs away on the right side, so price TRAVELLED
                          to VWAP rather than starting there
      did not linger      at most `max_dwell` earlier candles closed within
                          `band_atr` of VWAP. Hovering is exactly many closes
                          near VWAP, so this is the anti-hover rule
      snapped back        the touch candle reached VWAP, then closed at least one
                          band away on the right side, in the right colour, in the
                          outer 40% of its own range: a rejection, not a doji

    ATR rather than percent, so "well away" means the same thing on a quiet $300
    name and a volatile $10 one.
    """
    tf, n = int(p["tf"]), int(p["lookback"])
    if not c.series.completed[tf]:
        return None
    atr = _f(getattr(c.state, "atr_d1", None))
    if not atr or atr <= 0:
        return None
    cs = _session_completed(c, tf, n)
    # Only candles from this date and session share a VWAP anchor. A previous
    # session's approach cannot supply the distance/dwell for today's V.
    if len(cs) < n or any(x.get("vwap") is None for x in cs):
        return None
    band, away = float(p["band_atr"]) * atr, float(p["away_atr"]) * atr
    *prior, t = cs
    v = t["vwap"]
    rng = t["high"] - t["low"]
    if rng <= 0:
        return None
    dwell = sum(1 for x in prior if abs(x["close"] - x["vwap"]) <= band)
    if dwell > int(p["max_dwell"]):
        return None
    key = f"vv:{tf}:{t.get('key')}:{opt}"
    if opt == "support":
        came = max(x["close"] - x["vwap"] for x in prior) >= away
        touched = t["low"] <= v + band
        snapped = (t["close"] >= v + band and t["close"] > t["open"]
                   and (t["close"] - t["low"]) >= 0.6 * rng)
        if came and touched and snapped and c.once(key):
            return Fire("long", v, f"V off VWAP {v:.2f} on {TF_LABEL.get(tf, str(tf) + ' Min')}")
    else:
        came = max(x["vwap"] - x["close"] for x in prior) >= away
        touched = t["high"] >= v - band
        snapped = (t["close"] <= v - band and t["close"] < t["open"]
                   and (t["high"] - t["close"]) >= 0.6 * rng)
        if came and touched and snapped and c.once(key):
            return Fire("short", v, f"V rejected at VWAP {v:.2f} on {TF_LABEL.get(tf, str(tf) + ' Min')}")
    return None


_RANGE_BASELINE_CANDLES = 20


def range_width_limit(c: "EvalCtx", cs: list[dict], tf: int, n: int, unit: int,
                      width: float) -> Optional[float]:
    """The widest a range may be, in PRICE, for the chosen unit. None = cannot tell.

    0  % of price       width percent of the range's midpoint (the original rule)
    1  x daily ATR      width times the stock's daily ATR
    2  x avg candle     width times the average high-low of the
                        _RANGE_BASELINE_CANDLES candles BEFORE the range. Before,
                        not including: the range's own quiet candles would drag
                        the baseline down and make every range look normal.
    """
    hi = max(x["high"] for x in cs)
    lo = min(x["low"] for x in cs)
    if unit == 1:
        atr = _f(getattr(c.state, "atr_d1", None))
        return width * atr if atr and atr > 0 else None
    if unit == 2:
        allc = _session_completed(c, tf)
        base = allc[-(n + _RANGE_BASELINE_CANDLES):-n] if len(allc) > n else []
        if len(base) < _RANGE_BASELINE_CANDLES // 2:
            return None
        avg = sum(x["high"] - x["low"] for x in base) / len(base)
        return width * avg if avg > 0 else None
    return width / 100.0 * (hi + lo) / 2.0


@_impl("range_break")
def _t_range_break(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    n, tf = int(p["bars"]), int(p["tf"])
    cs = _session_completed(c, tf, n)
    if len(cs) < n:
        return None
    # Note the early returns below leave the latch untouched on purpose: a bar
    # with no qualifying range is not evidence that price came back inside one.
    hi = max(x["high"] for x in cs)
    lo = min(x["low"] for x in cs)
    mid = (hi + lo) / 2.0
    if mid <= 0:
        return None
    # Tight enough to be a consolidation rather than just the last N candles.
    unit = int(p.get("width_unit", 0) or 0)
    limit = range_width_limit(c, cs, tf, n, unit, float(p["max_range_pct"]))
    if limit is None or hi - lo > limit:
        return None
    # Evaluation happens on every 1-minute bar, including minute 1 of an
    # incomplete higher-timeframe candle. Convert the completed range candles'
    # volume to a per-minute baseline before comparing like time units.
    vols = [float(x.get("volume") or 0.0) for x in cs]
    avg = sum(vols) / (len(vols) * tf) if vols else 0.0
    if avg <= 0 or float(c.bar.get("volume") or 0.0) < avg * float(p["vol_mult"]):
        return None
    px = float(c.bar["close"])
    # edge(), not once(): the window slides forward with price, so a trend would
    # keep presenting a fresh "range" and re-fire on every bar. Latching on
    # "outside the range" gives one alert per exit, and re-arms only when price
    # comes back inside. The key carries every threshold, not just the candles:
    # two setups on the same candles with different widths or volume reach this
    # line on different bars, and a shared latch let the first one silence the
    # other.
    key = f"rb:{tf}:{n}:{unit}:{float(p['max_range_pct'])}:{float(p['vol_mult'])}"
    if opt == "up":
        if c.edge(key + ":up", px > hi):
            return Fire("long", hi, f"broke {n}x{tf}min range high {hi:.2f} on volume")
        return None
    if c.edge(key + ":dn", px < lo):
        return Fire("short", lo, f"broke {n}x{tf}min range low {lo:.2f} on volume")
    return None


@_impl("ema_cross_ema")
def _t_ece(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    tf, fast_n, slow_n = int(p["tf"]), int(p["fast"]), int(p["slow"])
    if fast_n >= slow_n:
        return None                      # a "fast" EMA at or above the slow one is a typo
    fv = c.series.ema(tf, fast_n).value
    sv = c.series.ema(tf, slow_n).value
    if fv is None or sv is None:
        return None
    lbl = TF_LABEL.get(tf, f"{tf} Min")
    # Separate latches per direction: a setup may select only one of them, and
    # the two must not share an edge state.
    if opt == "up":
        if c.edge(f"ece:{tf}:{fast_n}:{slow_n}:up", fv > sv):
            return Fire("long", fv, f"EMA({fast_n}) crossed above EMA({slow_n}) on {lbl}")
        return None
    if c.edge(f"ece:{tf}:{fast_n}:{slow_n}:dn", fv < sv):
        return Fire("short", fv, f"EMA({fast_n}) crossed below EMA({slow_n}) on {lbl}")
    return None


@_impl("running")
def _t_running(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    pc = c.prev_close
    if not pc:
        return None
    unit = int(p.get("width_unit", 0) or 0)
    move = c.close - pc
    if unit == 1:
        atr = _f(getattr(c.state, "atr_d1", None))
        if not atr or atr <= 0:
            return None
        chg, label = move / atr, "x ATR"
    elif unit == 2:
        # the last completed 1-min candles; the bar being judged is still the
        # partial candle, so it cannot inflate its own baseline
        base = c.series.last_completed(1, _RUNNING_BASELINE_CANDLES)             or list(c.series.candles[1])
        if len(base) < _RUNNING_BASELINE_CANDLES // 2:
            return None
        avg = sum(x["high"] - x["low"] for x in base) / len(base)
        if avg <= 0:
            return None
        chg, label = move / avg, "x avg candle"
    else:
        chg, label = move / pc * 100.0, "%"
    thr = float(p["min_pct"])
    if opt == "up" and chg >= thr:
        return Fire("long", chg, f"+{chg:.2f}{label} in 60s")
    if opt == "down" and chg <= -thr:
        return Fire("short", chg, f"{chg:.2f}{label} in 60s")
    return None


_RUNNING_BASELINE_CANDLES = 20


@_impl("pct_change")
def _t_pct(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    ref = c.level("prior_close")
    pc = c.prev_close
    if not ref or pc is None:
        return None
    cur = (c.close / ref - 1) * 100.0
    prev = (pc / ref - 1) * 100.0
    thr = float(p["pct"])
    if opt == "up" and prev < thr <= cur:
        return Fire("long", cur, f"+{cur:.1f}% from close")
    if opt == "down" and prev > -thr >= cur:
        return Fire("short", cur, f"{cur:.1f}% from close")
    return None


@_impl("gap")
def _t_gap(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    if c.session != "rth":
        return None
    ref = c.level("prior_close")
    op = c.series.session_open()
    if not ref or op is None:
        return None
    gap = (op / ref - 1) * 100.0
    thr = float(p["min_pct"])
    if opt == "up" and gap >= thr and c.once("gap:up"):
        return Fire("long", gap, f"gap +{gap:.1f}%")
    if opt == "down" and gap <= -thr and c.once("gap:down"):
        return Fire("short", gap, f"gap {gap:.1f}%")
    return None


@_impl("rvol_cross")
def _t_rvol(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    rv = _f(getattr(c.state, "rvol", None))
    if rv is None:
        return None
    thr = float(p["threshold"])
    if c.edge("rvol_cross", rv >= thr):
        return Fire("neutral", rv, f"RVOL {rv:.1f}x")
    return None


@_impl("rs_spy")
def _t_rs(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    mom = _f(getattr(c.state, "mom_15m_pct", None))
    if mom is None or c.spy_mom_15m is None:
        return None
    rs = mom - c.spy_mom_15m
    thr = float(p["pct"])
    if opt == "strong" and c.edge("rs:strong", rs >= thr):
        return Fire("long", rs, f"RS +{rs:.2f}% vs SPY (15m)")
    if opt == "weak" and c.edge("rs:weak", rs <= -thr):
        return Fire("short", rs, f"RW {rs:.2f}% vs SPY (15m)")
    return None


@_impl("momentum_burst")
def _t_burst(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
    atr = _f(getattr(c.state, "atr_d1", None))
    h, l, o, cl = float(c.bar["high"]), float(c.bar["low"]), float(c.bar["open"]), c.close
    rng = h - l
    if not atr or rng <= 0:
        return None
    big = rng >= float(p["range_mult"]) * atr
    pos = (cl - l) / rng
    if opt == "up" and cl >= o and big and pos >= float(p["close_pct"]) / 100.0:
        return Fire("long", rng / atr, f"burst {rng / atr:.2f}x ATR")
    if opt == "down" and cl <= o and big and (1 - pos) >= float(p["close_pct"]) / 100.0:
        return Fire("short", rng / atr, f"burst {rng / atr:.2f}x ATR")
    return None


def _classic_cross_factory(study_id: str, label: str, study: str,
                           column: str) -> TriggerFn:
    def _cross(c: EvalCtx, opt: str, p: dict) -> Optional[Fire]:
        tf = int(p["tf"])
        if tf not in c.series.completed or not c.series.completed[tf]:
            return None
        values = c.series.study(tf, study)
        if len(values) < 2:
            return None
        before, after = _f(values[column].iloc[-2]), _f(values[column].iloc[-1])
        if before is None or after is None:
            return None
        level = float(p["level"])
        crossed = before <= level < after if opt == "up" else before >= level > after
        if not crossed:
            return None
        candle_key = c.series.candles[tf][-1].get("key")
        if not c.once(f"ta:{study_id}:{tf}:{level}:{opt}:{candle_key}"):
            return None
        direction = "long" if opt == "up" else "short"
        return Fire(direction, after, f"{label} crossed {opt} {level:g} on {tf} min")
    return _cross


for _study_id, _label, _study, _column, _default in _CLASSIC_TRIGGER_SPECS:
    _IMPL[f"ta_{_study_id}_cross"] = _classic_cross_factory(
        _study_id, _label, _study, _column)


# external pass-through: setup:*
def evaluate(tid: str, ctx: EvalCtx, opt: str, params: dict) -> Optional[Fire]:
    t = BY_ID.get(tid)
    if t is None:
        return None
    if ctx.session not in t.sessions:
        return None
    if t.source == "system":
        if tid in ctx.external:
            d = t.direction if t.direction in ("long", "short", "neutral") else "neutral"
            return Fire(d, None, t.name)
        return None
    fn = _IMPL.get(tid)
    if fn is None:
        return None
    p = {q.key: q.default for q in t.params}
    p.update({k: v for k, v in (params or {}).items() if k in p})
    previous_scope = ctx.memory_scope
    ctx.memory_scope = json.dumps([tid, opt, p], sort_keys=True, separators=(",", ":"))
    try:
        return fn(ctx, opt, p)
    finally:
        ctx.memory_scope = previous_scope


def trigger_key(tid: str, opt: str) -> str:
    return f"{tid}:{opt}" if opt else tid


def describe(tid: str, opt: str, params: dict) -> str:
    """Human sentence for the Summary tab."""
    t = BY_ID.get(tid)
    if t is None:
        return tid
    label = t.name
    if opt:
        for o in t.options:
            if o.key == opt:
                label = f"{t.name} · {o.label}"
                break
    if t.params:
        p = {q.key: q.default for q in t.params}
        p.update({k: v for k, v in (params or {}).items() if k in p})
        def _show(q):
            v = p[q.key]
            if q.choice_labels and q.choices:
                i = min(range(len(q.choices)), key=lambda k: abs(q.choices[k] - float(v)))
                return f"{q.label} {q.choice_labels[i]}"
            return f"{q.label} {v}{(' ' + q.unit) if q.unit else ''}"
        bits = [_show(q) for q in t.params]
        label += " (" + ", ".join(bits) + ")"
    return label
