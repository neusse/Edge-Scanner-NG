"""Universe conditions: the screening vocabulary a profile is built from.

A **condition** is one row of a universe profile: a field, an operator and a
value, e.g. `avg_dollar_vol_20d >= 50_000_000` or `rel_vol >= 2.0 on 2 min`.
Profiles (scanner/profiles.py) are named, reusable AND-lists of these, and a
setup points at one profile. When a setup would fire, its profile is checked
before the alert is emitted.

This replaces screening that used to live in `scripts/build_universe.py` as CLI
flags. The base universe becomes a floor (the tradable symbol list), and the
actual screen moves here, where it is editable per setup from the dashboard
without a rebuild or a restart.

Two classes of condition, and the difference drives the caching in
scanner/profiles.py:

  STATIC   symbol properties fixed for the session (price, adv20, dollar
           volume, ATR%, float, sector). Resolved once per session per symbol
           into a member set, so checking them is a hash lookup.
  DYNAMIC  changes every bar (RVOL, relative N-min volume, candle streaks,
           distance from VWAP/EMA9). Resolved at fire time.

## Edge vs level

Triggers in `scanner/trigger_catalog.py` have EDGE semantics: they fire once on
a transition and latch in `series.mem`. Conditions have LEVEL semantics: "is
this true right now", asked repeatedly, no latching. `ConditionCtx` therefore
deliberately has no `edge()` and no `once()`, so a condition physically cannot
corrupt a trigger's latch. The shared arithmetic lives in `trigger_catalog`
(`candle_rel_volume`, `rolling_rel_volume`, `consec_streak`) and both sides
call it.

## Missing values

`availability` decides what an unresolvable value does:

  "block"  fail closed. Matches the existing gates, which return passed=False
           on None (e.g. `gates.gate_rvol`).
           Execution clients may trade this feed, so an unvetted symbol must
           not fire.
  "pass"   fail open, with the reason recorded. Reserved for the yfinance
           fundamentals, which are routinely missing and whose background
           prefetch takes a long time over a wide universe. Failing closed
           there would silence the scanner for the first hour of every day.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from scanner.gates import GateCheck
from scanner.indicators.adx import adx as compute_adx
from scanner.trigger_catalog import (
    LEVELS,
    TF_LABEL,
    candle_rel_volume,
    consec_streak,
    level_value,
    rolling_rel_volume,
)

log = logging.getLogger(__name__)

# Operators. Kept deliberately small: a universe screen is a set of floors and
# ceilings, not a query language.
OPS: dict[str, Callable[[float, float], bool]] = {
    "gte": lambda a, b: a >= b,
    "lte": lambda a, b: a <= b,
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
}
OP_LABEL = {"gte": ">=", "lte": "<=", "gt": ">", "lt": "<"}


class ConditionError(ValueError):
    """Raised when a condition cannot be normalized."""


@dataclass
class ConditionCtx:
    """What a condition resolver may read.

    Mirrors `trigger_catalog.EvalCtx` minus `edge()` and `once()`. That absence
    is the point: it makes latch corruption a type error rather than a code
    review question.
    """
    state: Any                                  # SymbolState
    series: Any = None                          # SymbolSeries, may be None for static-only
    bar: Optional[dict] = None
    session: str = "rth"
    direction: Optional[str] = None             # the candidate alert's direction
    fundamentals: Optional[dict] = None         # scanner.fundamentals row, may be None
    regime: Optional[Any] = None                # MarketRegime, for market_align
    quote: Optional[dict] = None                 # shared QuoteBook snapshot, when available

    @property
    def dir_sign(self) -> float:
        """+1 for a long candidate, -1 for a short, +1 when unknown.

        Several of the gates are the same question asked in two mirrored
        forms ("above VWAP for longs, below for shorts"). Signing the value by
        the direction turns each of them into one condition with a single
        threshold instead of two conditions that must be kept in step.
        """
        return -1.0 if self.direction == "short" else 1.0

    @property
    def price(self) -> Optional[float]:
        """Live price, falling back to the prior close.

        The fallback matters at warmup: static conditions are resolved into
        member sets before any bar has arrived, and `_last_close` is None until
        the first one. Prior close is the right answer there, and it is what a
        universe screen means by "price" anyway.
        """
        if self.bar is not None:
            return float(self.bar["close"])
        v = _f(getattr(self.state, "_last_close", None))
        return v if v is not None else _f(getattr(self.state, "prior_close", None))


@dataclass(frozen=True)
class OptionDef:
    key: str
    label: str


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
    # When set, the param may only take one of these values and the panel shows
    # a dropdown. For candle sizes: typing "3" into a timeframe box produces a
    # 3-minute candle nobody asked for, and a free box hides what is allowed.
    choices: tuple[float, ...] = ()


@dataclass(frozen=True)
class ConditionDef:
    """One screenable field. Mirrors `trigger_catalog.TriggerDef`."""
    id: str
    name: str
    category: str                       # Liquidity | Volatility | Volume | Price & levels | Fundamentals
    desc: str
    kind: str                           # "static" | "dynamic"
    resolve: Callable[[ConditionCtx, str, dict], Optional[float]]
    unit: str = ""
    ops: tuple[str, ...] = ("gte", "lte")
    default_op: str = "gte"
    default_value: float = 0.0
    min: Optional[float] = None
    max: Optional[float] = None
    step: Optional[float] = None
    options: tuple[OptionDef, ...] = ()
    option_label: str = ""
    default_option: str = ""
    params: tuple[ParamDef, ...] = ()
    availability: str = "block"         # "block" (fail closed) | "pass" (fail open)
    phrase: str = ""                    # optional tail for describe(), formatted with params
    # Extra tails keyed by param: appended only when that param is non-zero.
    phrase_if: dict = field(default_factory=dict)
    # Overrides `availability` for a missing value when it returns True: "not
    # resolvable YET" (warming up) can pass where "should exist but does not"
    # still blocks. Kept off to_json; it is engine behaviour, not a setting.
    warming_up: Optional[Callable[["ConditionCtx"], bool]] = None

    def to_json(self) -> dict:
        return {
            "id": self.id, "name": self.name, "category": self.category, "desc": self.desc,
            "kind": self.kind, "unit": self.unit, "ops": list(self.ops),
            "default_op": self.default_op, "default_value": self.default_value,
            "min": self.min, "max": self.max, "step": self.step,
            "option_label": self.option_label, "default_option": self.default_option,
            "options": [{"key": o.key, "label": o.label} for o in self.options],
            "params": [{"key": p.key, "label": p.label, "default": p.default, "min": p.min,
                        "max": p.max, "step": p.step, "unit": p.unit, "desc": p.desc,
                        "choices": list(p.choices)}
                       for p in self.params],
            "availability": self.availability,
        }


CATALOG: dict[str, ConditionDef] = {}


def _add(d: ConditionDef) -> ConditionDef:
    CATALOG[d.id] = d
    return d


def _f(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        return None if f != f else f          # drop NaN
    except (TypeError, ValueError):
        return None


# Candle sizes offered as a dropdown. 2 is kept because saved setups use it;
# dropping it would silently snap those setups to 1 or 5.
_TF_CHOICES = (1.0, 2.0, 3.0, 5.0, 15.0)

# ── static: liquidity and size ───────────────────────────────────────────────

_add(ConditionDef(
    "price", "Last price", "Liquidity",
    "Share price. The floor that keeps sub-dollar names out of a setup.",
    "static", lambda c, o, p: c.price,
    unit="$", default_op="gte", default_value=10.0, min=0, max=10000, step=0.5,
))

_add(ConditionDef(
    "avg_vol_20d", "Avg volume (20d)", "Liquidity",
    "20-day average daily volume in shares. Thin names tend to trade poorly.",
    "static", lambda c, o, p: _f(getattr(c.state, "adv20", None)),
    unit="shares", default_op="gte", default_value=1_000_000, min=0, step=100_000,
))

_add(ConditionDef(
    "avg_dollar_vol_20d", "Avg dollar volume (20d)", "Liquidity",
    "adv20 x prior close. The liquidity measure the old universe screen used, "
    "and a better size proxy than market cap for a trading scanner.",
    "static",
    lambda c, o, p: (lambda a, pc: a * pc if a is not None and pc else None)(
        _f(getattr(c.state, "adv20", None)), _f(getattr(c.state, "prior_close", None))),
    unit="$", default_op="gte", default_value=10_000_000, min=0, step=1_000_000,
))

_add(ConditionDef(
    "atr_pct", "ATR % (daily)", "Volatility",
    "Wilder daily ATR as a percent of prior close. A scanner wants movers, and "
    "this is the one screen dimension that is not a liquidity proxy. "
    "NOTE: build_universe.py's atr_pct column is a simple average range, not "
    "Wilder, so the two numbers differ for the same symbol.",
    "static",
    lambda c, o, p: (lambda a, pc: a / pc * 100 if a is not None and pc else None)(
        _f(getattr(c.state, "atr_d1", None)), _f(getattr(c.state, "prior_close", None))),
    unit="%", default_op="gte", default_value=1.0, min=0, max=100, step=0.1,
))

# ── static: fundamentals (yfinance, routinely missing -> fail open) ──────────

def _fund(key: str) -> Callable[[ConditionCtx, str, dict], Optional[float]]:
    def _r(c: ConditionCtx, o: str, p: dict) -> Optional[float]:
        return _f((c.fundamentals or {}).get(key))
    return _r


_add(ConditionDef(
    "market_cap", "Market cap", "Fundamentals",
    "From the yfinance fundamentals cache. Missing values PASS: the background "
    "prefetch takes a long time over a wide universe.",
    "static", _fund("market_cap"),
    unit="$", ops=("gte", "lte"), default_op="gte", default_value=300_000_000,
    min=0, step=50_000_000, availability="pass",
))

_add(ConditionDef(
    "float_shares", "Float", "Fundamentals",
    "Free-float shares. For a low-float scanner this is the primary screen, but "
    "the yfinance figure is stale often enough that it should not be trusted "
    "alone. Missing values PASS.",
    "static", _fund("float_shares"),
    unit="shares", ops=("gte", "lte"), default_op="lte", default_value=20_000_000,
    min=0, step=1_000_000, availability="pass",
))

_add(ConditionDef(
    "short_pct_float", "Short % of float", "Fundamentals",
    "Short interest as a percent of float. Missing values PASS.",
    "static", _fund("short_pct_float"),
    unit="%", ops=("gte", "lte"), default_op="gte", default_value=10.0,
    min=0, max=100, step=0.5, availability="pass",
))

# ── dynamic: volume ──────────────────────────────────────────────────────────

_add(ConditionDef(
    "session_volume", "Volume today", "Volume",
    "Cumulative RTH volume so far this session. Note this is a no-op against a "
    "base universe already screened on a high adv20; it earns its keep once the "
    "universe widens.",
    "dynamic", lambda c, o, p: _f(getattr(c.state, "session_volume", None)),
    unit="shares", default_op="gte", default_value=500_000, min=0, step=100_000,
))

_add(ConditionDef(
    "rvol", "Relative volume (time of day)", "Volume",
    "Session volume against the 20-day profile for this time of day. 1.0 = a "
    "normal day's pace. This is the scanner's per-symbol slot method, which "
    "reads about 16% higher than a market-average method.",
    "dynamic", lambda c, o, p: _f(getattr(c.state, "rvol", None)),
    unit="x", default_op="gte", default_value=1.0, min=0, max=50, step=0.05,
))


def _resolve_opening_direction(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    direction = _f(getattr(c.state, "opening_candle_direction", None))
    if direction is None:
        return None
    return direction * c.dir_sign if opt == "trade" else direction


_add(ConditionDef(
    "opening_direction_m5", "Opening 5-min candle direction", "Price & levels",
    "Direction of the completed 09:30-09:35 ET candle. In the trade direction, "
    "1 means a bullish opening candle for a long or a bearish opening candle "
    "for a short; a doji is 0 and does not pass.",
    "dynamic", _resolve_opening_direction,
    unit="", ops=("gte", "lte"), default_op="gte", default_value=1.0,
    min=-1, max=1, step=1,
    options=(OptionDef("trade", "In the trade direction"), OptionDef("raw", "Signed")),
    option_label="Sign", default_option="trade",
))


_add(ConditionDef(
    "opening_rvol_m5", "Opening 5-min relative volume", "Volume",
    "Volume in the completed 09:30-09:35 ET candle divided by the average "
    "volume in that same five-minute slot from the loaded history.",
    "dynamic", lambda c, o, p: _f(getattr(c.state, "opening_rvol_m5", None)),
    unit="x", default_op="gte", default_value=1.0, min=0, max=100, step=0.1,
))


def _resolve_opening_rvol_rank(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    coverage = _f(getattr(c.state, "opening_rvol_coverage", None))
    if coverage is None or coverage * 100.0 < float(p.get("min_coverage", 80)):
        return None
    return _f(getattr(c.state, "opening_rvol_rank", None))


_add(ConditionDef(
    "opening_rvol_rank", "Opening-volume universe rank", "Volume",
    "Rank by first-five-minute relative volume across the loaded scanner "
    "universe: 1 is highest. It blocks until the configured share of symbols "
    "has a completed opening-volume value, preventing a partial early ranking.",
    "dynamic", _resolve_opening_rvol_rank,
    unit="rank", ops=("lte", "gte"), default_op="lte", default_value=20.0,
    min=1, max=1000, step=1,
    params=(ParamDef("min_coverage", "Universe data ready", 80, 1, 100, 1, "%",
                     "Minimum share of loaded symbols with an opening-volume value."),),
    phrase="after {min_coverage}% universe coverage",
))


def _resolve_rel_vol(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """Rolling relative volume as a PERCENT (200 = twice normal), the usual way
    scanners phrase it.

    Uses the every-bar rolling window rather than the completed-candle version,
    so the value is never stale by up to tf-1 minutes. Falls back to the
    completed-candle math on 5 min and above, where the ring is seeded from
    5-minute history and therefore has a baseline from the first candle.
    """
    if c.series is None:
        return None
    tf = int(opt or 2)
    lookback = int(p.get("lookback", 10))
    v = rolling_rel_volume(c.series, tf, lookback, session=c.session)
    if v is None and tf >= 5:
        v = candle_rel_volume(c.series, tf, lookback)
    return None if v is None else v * 100.0


_add(ConditionDef(
    "rel_vol", "Relative N-min volume", "Volume",
    "Volume of the trailing N minutes against the mean of the preceding blocks, "
    "as a percent (200 = twice normal). The 1- and 2-minute rings are "
    "session-only, so those timeframes have no baseline until roughly 09:52.",
    "dynamic", _resolve_rel_vol,
    unit="%", default_op="gte", default_value=200.0, min=0, max=10000, step=10,
    options=tuple(OptionDef(str(t), TF_LABEL[t]) for t in (1, 2, 5, 15)),
    option_label="Timeframe", default_option="2",
    params=(ParamDef("lookback", "Baseline blocks", 10, 2, 60, 1, "blocks",
                     "How many preceding N-minute blocks form the average."),),
))

# ── dynamic: price action ────────────────────────────────────────────────────

def _resolve_streak(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """Consecutive-candle run length.

    Options pick what the number means, because a "require a run" filter and
    an "already extended, do not chase" filter are phrased with the same words
    and only the sign tells them apart:

      green   +N for a green run, 0 otherwise  (require green momentum)
      red     +N for a red run,   0 otherwise  (require red momentum)
      abs     run length regardless of colour  (anti-extension: pair with <=)
    """
    if c.series is None:
        return None
    n = consec_streak(
        c.series,
        int(opt.split(":")[-1]) if ":" in opt else int(p.get("tf", 2)),
        c.session,
    )
    if n is None:
        return None
    which = opt.split(":")[0] if ":" in opt else opt
    if which == "green":
        return float(max(n, 0))
    if which == "red":
        return float(max(-n, 0))
    return float(abs(n))


_add(ConditionDef(
    "consec_candles", "Consecutive candles", "Price & levels",
    "Length of the current run of same-colour candles. Use 'Either colour' with "
    "a <= operator as an anti-extension filter (do not chase something that has "
    "already run), or green/red with >= to require momentum.",
    "dynamic", _resolve_streak,
    unit="candles", ops=("gte", "lte"), default_op="lte", default_value=3,
    min=0, max=50, step=1,
    options=(OptionDef("green", "Green run"), OptionDef("red", "Red run"),
             OptionDef("abs", "Either colour")),
    option_label="Direction", default_option="abs",
    params=(ParamDef("tf", "Timeframe", 2, 1, 15, 1, "min",
                     "Candle size the run is counted on.", choices=_TF_CHOICES),),
))


def _resolve_dist_vwap(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    d = _f(getattr(c.state, "dist_vwap_pct", None))
    if d is None:
        return None
    if opt == "above":
        return d
    if opt == "below":
        return -d
    if opt == "trade":
        # The VWAP-side gate: above VWAP for longs, below for shorts. Signed
        # by direction so one ">= 0" says it, rather than a second condition.
        return d * c.dir_sign
    return abs(d)


_add(ConditionDef(
    "dist_vwap_pct", "Distance from VWAP", "Price & levels",
    "How far price sits from session VWAP. 'Either side' is the absolute "
    "distance; 'Above'/'Below' are signed so a >= reads naturally.",
    "dynamic", _resolve_dist_vwap,
    unit="%", default_op="gte", default_value=2.0, min=0, max=100, step=0.1,
    options=(OptionDef("abs", "Either side"), OptionDef("above", "Above VWAP"),
             OptionDef("below", "Below VWAP"),
             OptionDef("trade", "In the trade direction")),
    option_label="Side", default_option="abs",
))


def _resolve_dist_ema9(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    tf = int(opt or 5)
    if tf == 5:
        d = _f(getattr(c.state, "dist_ema9_pct", None))
        if d is not None:
            return abs(d)
    if c.series is None:
        return None
    ema = c.series.ema(tf, 9).value
    px = c.price
    if ema is None or not px:
        return None
    return abs((px - ema) / ema * 100.0)


_add(ConditionDef(
    "dist_ema9_pct", "Distance from EMA(9)", "Price & levels",
    "Absolute distance from the 9-period EMA on the chosen timeframe. 5 min "
    "reads the value SymbolState already maintains; other timeframes come from "
    "the multi-timeframe series.",
    "dynamic", _resolve_dist_ema9,
    unit="%", default_op="gte", default_value=2.0, min=0, max=100, step=0.1,
    options=tuple(OptionDef(str(t), TF_LABEL[t]) for t in (1, 5, 15)),
    option_label="Timeframe", default_option="5",
))

def _resolve_vwap_hold(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """How many of the last N completed candles closed on one side of VWAP.

    The difference between VWAP acting as SUPPORT and price merely touching it:
    a stock that has closed above VWAP candle after candle is treating it as a
    floor, while one crossing it every few minutes is just oscillating around
    it, and a "bounce" there means nothing. A common VWAP support/resistance
    rule asks exactly this with 4 of the last 5 five-minute bars; this makes it
    a parameter any setup can use.

    Counts only candles from TODAY's regular session. Seeded history and
    premarket candles carry no VWAP (it accumulates in regular hours only), and
    VWAP resets each session, so comparing a candle to yesterday's would be
    meaningless. Until N such candles exist it is unavailable and fails closed:
    on 5-minute candles with a lookback of 5 that is 09:55, and a setup cannot
    claim VWAP is holding before it has had the time to hold.
    """
    if c.series is None:
        return None
    tf, n = int(p.get("tf", 5)), int(p.get("lookback", 5))
    cs = c.series.last_completed(tf, n)
    if len(cs) < n or any(x.get("vwap") is None for x in cs):
        return None
    # A close a cent above VWAP is a stock parked on the line, not VWAP acting as
    # support. The margin makes a candle count only when it closed clearly away.
    margin_pct = float(p.get("margin", 0) or 0)
    m = 0.0
    if margin_pct > 0:
        atr = _f(getattr(c.state, "atr_d1", None))
        if not atr or atr <= 0:
            return None
        m = atr * margin_pct / 100.0
    above = sum(1 for x in cs if x["close"] > x["vwap"] + m)
    below = sum(1 for x in cs if x["close"] < x["vwap"] - m)
    if opt == "above":
        return float(above)
    if opt == "below":
        return float(below)
    return float(below if c.direction == "short" else above)


_add(ConditionDef(
    "vwap_hold", "Held above / below VWAP", "Price & levels",
    "How many of the last N completed candles closed on the trade's side of VWAP: "
    "above it for a long, below it for a short. Set it equal to the lookback to "
    "require every one (VWAP as a true support, not a level price keeps crossing). "
    "A typical VWAP support/resistance rule uses 4 of the last 5 five-minute candles. "
    "Needs N candles from today's session, so on 5-min candles it cannot pass "
    "before 09:55.",
    "dynamic", _resolve_vwap_hold,
    unit="", default_op="gte", default_value=5.0, min=0, max=60, step=1,
    phrase="of the last {lookback} candles on {tf} min",
    # appended only when the margin is set, so saved setups read exactly as before
    phrase_if={"margin": ", by at least {margin}% of daily ATR"},
    options=(OptionDef("trade", "In the trade direction"), OptionDef("above", "Above"),
             OptionDef("below", "Below")),
    option_label="Side", default_option="trade",
    params=(ParamDef("lookback", "Last N candles", 5, 2, 60, 1, "candles",
                     "How many completed candles to look back over."),
            ParamDef("tf", "Timeframe", 5, 1, 15, 1, "min",
                     "Candle size the count is taken on.", choices=_TF_CHOICES),
            ParamDef("margin", "By at least", 0, 0, 50, 0.5, "% ATR",
                     "A candle only counts when it closed at least this far from VWAP, as a percent "
                     "of daily ATR. 0 counts any close on the right side, which lets a stock "
                     "hovering on VWAP pass.")),
))


def _today_rth_candles(series: Any, tf: int) -> list[dict]:
    """Completed tf-candles from today's regular session that carry a VWAP."""
    day = getattr(series, "session_date", None)
    return [x for x in series.candles[tf]
            if x.get("vwap") is not None and x.get("key", (None, None))[0] == day
            and x.get("key", (None, None))[1] == "rth"]


def _resolve_vwap_day_pct(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """Percent of today's completed candles that closed on one side of VWAP.

    "Above VWAP all day" as a number, so 100 means every candle since the open
    and 90 tolerates the odd dip. Unavailable until today has a completed candle.
    """
    if c.series is None:
        return None
    tf = int(p.get("tf", 5))
    if tf not in c.series.candles:
        return None
    cs = _today_rth_candles(c.series, tf)
    if not cs:
        return None
    above = 100.0 * sum(1 for x in cs if x["close"] > x["vwap"]) / len(cs)
    below = 100.0 * sum(1 for x in cs if x["close"] < x["vwap"]) / len(cs)
    if opt == "above":
        return above
    if opt == "below":
        return below
    return below if c.direction == "short" else above


_add(ConditionDef(
    "vwap_day_pct", "Time above / below VWAP today", "Price & levels",
    "Percent of today's completed candles that closed on the trade's side of VWAP: "
    "above for a long, below for a short. 100 means every candle since the open. "
    "Early in the session there are few candles, so pair it with a Time of day "
    "condition when \"all day\" should mean something.",
    "dynamic", _resolve_vwap_day_pct,
    unit="%", default_op="gte", default_value=100.0, min=0, max=100, step=5,
    phrase="of today's candles on {tf} min",
    options=(OptionDef("trade", "In the trade direction"), OptionDef("above", "Above"),
             OptionDef("below", "Below")),
    option_label="Side", default_option="trade",
    params=(ParamDef("tf", "Timeframe", 5, 1, 15, 1, "min",
                     "Candle size the share is taken on.", choices=_TF_CHOICES),),
))


def _resolve_ema_hold(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """How many of the last N completed candles closed on one side of an EMA,
    each compared with the EMA as it stood at that candle.

    The EMA is recomputed across the candle ring on demand rather than kept per
    candle: conditions are only asked when an alert is about to fire, so this is
    rare, and it needs no new per-bar state.
    """
    if c.series is None:
        return None
    tf, period, n = int(p.get("tf", 5)), int(p.get("period", 9)), int(p.get("lookback", 3))
    if tf not in c.series.candles or n < 1 or period < 1:
        return None
    cs = list(c.series.candles[tf])
    if len(cs) < max(n, period):
        return None
    k = 2.0 / (period + 1)
    e = cs[0]["close"]
    emas = []
    for x in cs:
        e = x["close"] * k + e * (1 - k)
        emas.append(e)
    last = list(zip(cs[-n:], emas[-n:]))
    above = sum(1 for x, ev in last if x["close"] > ev)
    below = sum(1 for x, ev in last if x["close"] < ev)
    if opt == "above":
        return float(above)
    if opt == "below":
        return float(below)
    return float(below if c.direction == "short" else above)


_add(ConditionDef(
    "ema_hold", "Held above / below EMA", "Price & levels",
    "How many of the last N completed candles closed on the trade's side of an EMA: "
    "above for a long, below for a short. Set it equal to N to require every one.",
    "dynamic", _resolve_ema_hold,
    unit="", default_op="gte", default_value=3.0, min=0, max=60, step=1,
    phrase="of the last {lookback} candles vs EMA({period}) on {tf} min",
    options=(OptionDef("trade", "In the trade direction"), OptionDef("above", "Above"),
             OptionDef("below", "Below")),
    option_label="Side", default_option="trade",
    params=(ParamDef("period", "EMA period", 9, 2, 200, 1, "period"),
            ParamDef("lookback", "Last N candles", 3, 1, 60, 1, "candles"),
            ParamDef("tf", "Timeframe", 5, 1, 15, 1, "min", choices=_TF_CHOICES)),
))


def _resolve_time_et(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """The bar's CLOSE time in ET as HHMM (10:31 -> 1031), the time the Scanner
    window shows. HHMM compares correctly with >= and <= within a day."""
    if c.bar is None:
        return None
    try:
        import pandas as pd
        ts = pd.Timestamp(c.bar["timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        et = (ts + pd.Timedelta(minutes=1)).tz_convert("America/New_York")
        return float(et.hour * 100 + et.minute)
    except Exception:
        return None


_add(ConditionDef(
    "time_et", "Time of day (ET)", "Time",
    "When the alert's bar closed, in Eastern time, written as HHMM: 1030 is 10:30, "
    "1545 is 15:45. Use >= for 'not before' and <= for 'not after'; add it twice "
    "for a window.",
    "dynamic", _resolve_time_et,
    unit="hhmm", ops=("gte", "lte"), default_op="gte", default_value=1030.0, min=400, max=2000, step=5,
))


def _resolve_ema_stack(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """Separation between two EMAs, as a percent of the slower one.

    A trend filter such as "EMA(8) above EMA(21)" is a STATE the whole time,
    not an event on one bar, so it is a parameter and not a trigger. Pair it
    with an ordinary trigger (a VWAP cross, say) to get a trend-filtered
    version of that trigger without a separate trigger for it.
    """
    if c.series is None:
        return None
    tf, fast_n, slow_n = int(p.get("tf", 1)), int(p.get("fast", 8)), int(p.get("slow", 21))
    if fast_n >= slow_n:
        return None
    fv = c.series.ema(tf, fast_n).value
    sv = c.series.ema(tf, slow_n).value
    if fv is None or sv is None or not sv:
        return None
    sep = (fv - sv) / sv * 100.0
    if opt == "trade":
        return sep * c.dir_sign
    if opt == "below":
        return -sep
    return sep


_add(ConditionDef(
    "ema_stack", "EMA above EMA", "Price & levels",
    "How far the fast EMA sits above the slow one, as a percent of the slow. "
    "'In the trade direction' signs it, so >= 0 means the stack agrees with the "
    "trade: fast above slow on a long, below on a short. Raise the threshold to "
    "demand a wider separation than a bare crossover.",
    "dynamic", _resolve_ema_stack,
    unit="%", default_op="gte", default_value=0.0, min=-100, max=100, step=0.05,
    phrase="(EMA {fast} vs {slow} on {tf} min)",
    options=(OptionDef("trade", "In the trade direction"), OptionDef("above", "Fast above slow"),
             OptionDef("below", "Fast below slow")),
    option_label="Side", default_option="trade",
    params=(ParamDef("fast", "Fast EMA", 8, 2, 200, 1, "period"),
            ParamDef("slow", "Slow EMA", 21, 3, 200, 1, "period"),
            ParamDef("tf", "Timeframe", 1, 1, 60, 1, "min")),
))


def _resolve_adx(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """Latest Wilder ADX from completed candles on the chosen timeframe."""
    if c.series is None:
        return None
    tf, period = int(p.get("tf", 5)), int(p.get("period", 14))
    if tf not in c.series.candles or period < 2:
        return None
    cs = list(c.series.candles[tf])
    if len(cs) < 2 * period:
        return None
    try:
        import pandas as pd
        value = compute_adx(
            pd.Series([x["high"] for x in cs], dtype=float),
            pd.Series([x["low"] for x in cs], dtype=float),
            pd.Series([x["close"] for x in cs], dtype=float),
            period,
        ).iloc[-1]
    except (KeyError, TypeError, ValueError):
        return None
    return _f(value)


_add(ConditionDef(
    "adx", "ADX", "Trend strength",
    "Wilder Average Directional Index on completed candles. ADX measures trend "
    "strength, not direction; pair it with an EMA stack or another directional "
    "condition. It fails closed until two full periods of candles are available.",
    "dynamic", _resolve_adx,
    unit="", default_op="gte", default_value=30.0, min=0, max=100, step=1,
    phrase="on {tf} min, period {period}",
    params=(ParamDef("period", "Period", 14, 2, 50, 1, "candles"),
            ParamDef("tf", "Timeframe", 5, 1, 60, 1, "min",
                     choices=(1.0, 2.0, 5.0, 15.0, 30.0, 60.0))),
))


_add(ConditionDef(
    "gap_pct", "Gap from prior close", "Price & levels",
    "Session open against prior close. A strong noise filter: longs with "
    "|gap| under 1% tend to have no edge.",
    "dynamic", lambda c, o, p: (lambda g: abs(g) if o == "abs" and g is not None else g)(
        _f(getattr(c.state, "gap_pct", None))),
    unit="%", default_op="gte", default_value=1.0, min=-100, max=100, step=0.1,
    options=(OptionDef("abs", "Absolute"), OptionDef("signed", "Signed")),
    option_label="Sign", default_option="abs",
))


def _resolve_atr_extension_d1(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """Live price's distance from the prior daily EMA8 in ATR(14) units."""
    px = c.price
    ema8 = _f(getattr(c.state, "ema_8_d1", None))
    atr = _f(getattr(c.state, "atr_14_d1", None))
    if px is None or ema8 is None or atr is None or atr <= 0:
        return None
    value = (px - ema8) / atr
    if opt == "abs":
        return abs(value)
    if opt == "below":
        return -value
    if opt == "trade":
        return value * c.dir_sign
    return value


_add(ConditionDef(
    "atr_extension_d1", "Daily ATR extension", "Volatility",
    "Live price's distance from the prior daily EMA8, measured in Wilder "
    "ATR(14). Positive 'Above' values identify upside extension; positive "
    "'Below' values identify downside extension. 'In the trade direction' "
    "signs the value for long and short setups.",
    "dynamic", _resolve_atr_extension_d1,
    unit="x ATR", ops=("gt", "gte", "lt", "lte"), default_op="gte",
    default_value=2.0, min=-20, max=20, step=0.1,
    options=(OptionDef("trade", "In the trade direction"),
             OptionDef("above", "Above EMA8"), OptionDef("below", "Below EMA8"),
             OptionDef("abs", "Either side")),
    option_label="Side", default_option="trade",
))

_add(ConditionDef(
    "day_range_pos", "Position in day range", "Price & levels",
    "0 = at the low of day, 1 = at the high.",
    "dynamic", lambda c, o, p: _f(getattr(c.state, "day_range_pos", None)),
    unit="", ops=("gte", "lte"), default_op="lte", default_value=0.8,
    min=0, max=1, step=0.05,
))

_add(ConditionDef(
    "rth_chg_pct", "Change today", "Price & levels",
    "Percent move from the prior close, regular hours.",
    "dynamic", lambda c, o, p: (lambda v: abs(v) if o == "abs" and v is not None else v)(
        _f(getattr(c.state, "rth_chg_pct", None))),
    unit="%", default_op="gte", default_value=2.0, min=-100, max=100, step=0.5,
    options=(OptionDef("abs", "Absolute"), OptionDef("signed", "Signed")),
    option_label="Sign", default_option="abs",
))

_add(ConditionDef(
    "mom_15m_pct", "15-minute momentum", "Price & levels",
    "Percent move over the last 15 minutes.",
    "dynamic", lambda c, o, p: (lambda v: abs(v) if o == "abs" and v is not None else v)(
        _f(getattr(c.state, "mom_15m_pct", None))),
    unit="%", default_op="gte", default_value=1.0, min=-100, max=100, step=0.1,
    options=(OptionDef("abs", "Absolute"), OptionDef("signed", "Signed")),
    option_label="Sign", default_option="abs",
))


# ── dynamic: the shared gate vocabulary ──────────────────────────────────────
#
# These three expose the gates in scanner/gates.py (relative strength vs SPY,
# clear air to the next level, market alignment) as ordinary conditions.
# Together with rvol and dist_vwap_pct (In the trade direction) they cover
# every mandatory gate there except chart quality, so any setup can apply the
# same gates without a separate evaluator.


def _resolve_rrs(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """Intraday 5-minute RRS vs SPY.

    NOTE this is rrs_m5, not rrs_d1, despite the gate being named gate_rrs_d1:
    that function takes both and gates on m5 only, keeping d1 for display and
    scoring. Reading d1 here would silently gate on a different number.
    """
    v = _f(getattr(c.state, "rrs_m5", None))
    if v is None:
        return None
    return v * c.dir_sign if opt == "trade" else v


def _rrs_warming_up(c: ConditionCtx) -> bool:
    """True while the symbol has too few session 5-min bars for RRS to exist.

    Mirrors gates.gate_rrs_d1: a missing RRS passes until the symbol has
    GATE_RRS_WARMUP_5M_BARS five-minute bars (about 10:30), and blocks after
    that. Blocking from the open instead would mean a setup using it could not
    fire at all in the first hour, which is the whole of an opening-range
    window. A state with no bar history to count is unknown, not warming up,
    so it still blocks.
    """
    bars = getattr(c.state, "_stock_5m", None)
    if bars is None:
        return False
    from scanner.settings import settings as S
    return len(bars) < int(S.GATE_RRS_WARMUP_5M_BARS)


_add(ConditionDef(
    "rrs_m5", "Relative strength vs SPY", "Price & levels",
    "Intraday 5-minute real relative strength against SPY. 'In the trade "
    "direction' signs it, so >= 0 means the stock is stronger than SPY on a "
    "long and weaker on a short, the same test as the relative-strength gate. "
    "Needs about 12 five-minute bars before it resolves; until then it passes, "
    "as the gate does.",
    "dynamic", _resolve_rrs, warming_up=_rrs_warming_up,
    unit="", ops=("gt", "gte", "lt", "lte"), default_op="gt", default_value=0.0,
    min=-100, max=100, step=0.1,
    options=(OptionDef("trade", "In the trade direction"), OptionDef("raw", "Signed")),
    option_label="Sign", default_option="trade",
))


def _resolve_rrs_sector_m5(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    v = _f(getattr(c.state, "rrs_sector_m5", None))
    if v is None:
        return None
    return v * c.dir_sign if opt == "trade" else v


_add(ConditionDef(
    "rrs_sector_m5", "Relative strength vs sector ETF", "Price & levels",
    "Intraday 5-minute real relative strength against the stock's mapped "
    "sector ETF over the last hour. 'In the trade direction' signs it, so a "
    "positive value means sector-relative strength on a long and weakness on "
    "a short. Uses sector bars carried on the scanner's existing shared stream.",
    "dynamic", _resolve_rrs_sector_m5, warming_up=_rrs_warming_up,
    unit="", ops=("gt", "gte", "lt", "lte"), default_op="gt", default_value=0.0,
    min=-100, max=100, step=0.1,
    options=(OptionDef("trade", "In the trade direction"), OptionDef("raw", "Signed")),
    option_label="Sign", default_option="trade",
))


def _resolve_rrs_d1(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    v = _f(getattr(c.state, "rrs_d1", None))
    if v is None:
        return None
    return v * c.dir_sign if opt == "trade" else v


_add(ConditionDef(
    "rrs_d1", "Daily relative strength vs SPY", "Price & levels",
    "Real relative strength against SPY on daily bars over the last week, fixed "
    "for the session. 'In the trade direction' signs it, so >= 1 means a daily "
    "leader on a long and a daily laggard on a short. Pair it with the intraday "
    "one to find a leader that is weak this hour, or a laggard that is bouncing.",
    "dynamic", _resolve_rrs_d1,
    unit="", ops=("gt", "gte", "lt", "lte"), default_op="gte", default_value=1.0,
    min=-100, max=100, step=0.1,
    options=(OptionDef("trade", "In the trade direction"), OptionDef("raw", "Signed")),
    option_label="Sign", default_option="trade",
))


def _resolve_rrs_slope(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """Change in intraday RRS over the last `bars` five-minute bars."""
    tail = getattr(c.state, "rrs_m5_tail", None) or []
    n = int(p.get("bars", 3))
    if len(tail) <= n:
        return None
    d = tail[-1] - tail[-1 - n]
    return d * c.dir_sign if opt == "trade" else d


_add(ConditionDef(
    "rrs_slope", "Relative strength rising", "Price & levels",
    "How much the intraday relative strength vs SPY has changed over the last "
    "few 5-minute bars. 'In the trade direction' signs it, so a positive number "
    "means strength is building on a long and weakness is building on a short. "
    "Passes while relative strength is still warming up, as that one does.",
    "dynamic", _resolve_rrs_slope, warming_up=_rrs_warming_up,
    unit="", ops=("gt", "gte", "lt", "lte"), default_op="gte", default_value=0.3,
    min=-100, max=100, step=0.05,
    options=(OptionDef("trade", "In the trade direction"), OptionDef("raw", "Signed")),
    option_label="Sign", default_option="trade",
    params=(ParamDef("bars", "Over", 3, 1, 6, 1, "bars", "Five-minute bars the change is measured over."),),
    phrase="over {bars} bars",
))


def _resolve_dist_level(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    level = level_value(c.series, c.state, opt)
    px = c.price
    if level is None or not px or level <= 0:
        return None
    d = px - level
    side = int(p.get("side", 0))
    d = abs(d) if side == 0 else d * (1.0 if side > 0 else -1.0)
    if int(p.get("unit", 0)) == 1:
        atr = _f(getattr(c.state, "atr_d1", None))
        return d / atr * 100.0 if atr else None
    return d / level * 100.0


_add(ConditionDef(
    "dist_level", "Distance from a level", "Price & levels",
    "How far price is from a chosen level: yesterday's high or low, the "
    "premarket range, the open, a daily moving average. Side 0 is the distance "
    "either way (use <= to mean 'near'), 1 counts up from the level (>= 0 means "
    "above it), -1 counts down from it (>= 0 means below it). Unit 0 is percent "
    "of price, 1 is percent of the daily ATR.",
    "dynamic", _resolve_dist_level,
    unit="%", ops=("gte", "lte"), default_op="lte", default_value=0.5,
    min=-1000, max=1000, step=0.05,
    options=tuple(OptionDef(o.key, o.label) for o in LEVELS),
    option_label="Level", default_option="prior_high",
    params=(ParamDef("side", "Side", 0, -1, 1, 1, "", "0 either side, 1 above the level, -1 below it.",
                     choices=(-1.0, 0.0, 1.0)),
            ParamDef("unit", "Unit", 0, 0, 1, 1, "", "0 = % of price, 1 = % of daily ATR.",
                     choices=(0.0, 1.0))),
    phrase="away", phrase_if={"side": " (side {side})", "unit": " of daily ATR"},
))


def _resolve_void(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """Percent of clear air to the next 60-day level in the trade direction.

    Delegates to gates.gate_void rather than restating it: one implementation
    means the condition and the clear-air gate cannot drift apart, which is exactly
    what happened to the RVOL threshold between the engine and the old panel
    text. Imported lazily to keep scanner.gates out of this module's import
    graph.
    """
    # NOT daily_highs / daily_lows: SymbolState calls them *_60d, and getattr on
    # the wrong name returns None, which fails closed and silently blocks every
    # alert. That is exactly what happened until the replay comparison caught it.
    highs = getattr(c.state, "daily_highs_60d", None)
    lows = getattr(c.state, "daily_lows_60d", None)
    px = c.price
    if highs is None or lows is None or not px:
        return None
    try:
        from scanner.gates import gate_void
        return _f(gate_void(px, highs, lows, c.direction or "long").value)
    except Exception:
        return None


_add(ConditionDef(
    "void_pct", "Clear air to the next level", "Price & levels",
    "Distance to the nearest 60-day daily high above (long) or low below "
    "(short), as a percent of price. Room to run before the move meets the "
    "level that stopped it last time. 999 when there is no level in the way.",
    "dynamic", _resolve_void,
    unit="%", default_op="gte", default_value=1.0, min=0, max=1000, step=0.1,
))


def _resolve_market_align(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    """1 when the SPY regime is not against the trade, 0 when it is.

    Mirrors gates.gate_market_align, including the part the user guide gets
    wrong: NEUTRAL allows BOTH directions. Only an explicitly opposing regime
    blocks. Before the cutoff minute the gate is off, because early-session
    VWAP is too unstable to classify a regime from.
    """
    reg = c.regime
    if reg is None:
        return None
    bar_min = None
    if c.bar is not None:
        try:
            import pandas as pd
            ts = pd.Timestamp(c.bar["timestamp"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            et = ts.tz_convert("America/New_York")
            bar_min = et.hour * 60 + et.minute
        except Exception:
            bar_min = None
    if bar_min is not None and bar_min < int(p.get("from", 600)):
        return 1.0
    value = getattr(reg, "value", reg)
    if c.direction == "short":
        return 0.0 if value == "bullish" else 1.0
    return 0.0 if value == "bearish" else 1.0


_add(ConditionDef(
    "market_align", "Market not against the trade", "Price & levels",
    "The SPY regime must not oppose the trade after the cutoff time. A NEUTRAL "
    "market allows both directions; only an explicitly opposing one blocks. "
    "Before the cutoff it always passes, because early-session VWAP is too "
    "unstable to classify from.",
    "dynamic", _resolve_market_align,
    unit="bool", ops=("gte",), default_op="gte", default_value=1.0, min=0, max=1, step=1,
    params=(ParamDef("from", "Active from", 600, 570, 960, 5, "min ET",
                     "Minutes past midnight ET. 600 = 10:00."),),
))


def _spread_bps(c: ConditionCtx, opt: str, p: dict) -> Optional[float]:
    q = c.quote or {}
    if q.get("quality") != "valid" or q.get("delayed") is not False:
        return None
    age_limit = float(p.get("max_age_ms", 2000))
    if any(q.get(f"{side}_age_ms") is None or q[f"{side}_age_ms"] > age_limit
           for side in ("bid", "ask")):
        return None
    return q.get("spread_bps")


_add(ConditionDef(
    "spread_bps", "Live bid/ask spread", "Price & levels",
    "Current Schwab Level One ask minus bid, divided by midpoint, in basis points. "
    "Fails closed for missing, stale, delayed, invalid, locked or crossed quotes. "
    "Both sides must be within the configured market-time age; this is not depth or slippage.",
    "dynamic", _spread_bps, unit="bps", ops=("lte", "lt", "gte", "gt"),
    default_op="lte", default_value=20.0, min=0, max=10000, step=0.1,
    params=(ParamDef("max_age_ms", "Maximum side age", 2000, 100, 60000, 100, "ms"),),
))


# ── normalize + evaluate ─────────────────────────────────────────────────────

def normalize_condition(raw: dict) -> dict:
    """Validate and fill in one condition dict. Raises ConditionError."""
    if not isinstance(raw, dict):
        raise ConditionError(f"condition must be an object, got {type(raw).__name__}")
    cid = str(raw.get("id") or "").strip()
    d = CATALOG.get(cid)
    if d is None:
        raise ConditionError(f"unknown condition: {cid!r}")

    op = str(raw.get("op") or d.default_op)
    if op not in d.ops:
        raise ConditionError(f"{cid}: operator {op!r} not allowed (choose from {list(d.ops)})")

    try:
        value = float(raw.get("value", d.default_value))
    except (TypeError, ValueError):
        raise ConditionError(f"{cid}: value is not a number: {raw.get('value')!r}")
    if value != value:
        raise ConditionError(f"{cid}: value is not finite")
    if d.min is not None:
        value = max(d.min, value)
    if d.max is not None:
        value = min(d.max, value)

    option = str(raw.get("option") or d.default_option or "")
    if d.options:
        keys = {o.key for o in d.options}
        if option not in keys:
            raise ConditionError(f"{cid}: option {option!r} not in {sorted(keys)}")

    params: dict[str, float] = {}
    for pdef in d.params:
        try:
            v = float((raw.get("params") or {}).get(pdef.key, pdef.default))
        except (TypeError, ValueError):
            v = pdef.default
        if pdef.min is not None:
            v = max(pdef.min, v)
        if pdef.max is not None:
            v = min(pdef.max, v)
        if pdef.choices:
            v = min(pdef.choices, key=lambda c: abs(c - v))     # snap to an allowed value
        params[pdef.key] = v

    return {"id": cid, "op": op, "value": value, "option": option, "params": params}


def describe(cond: dict) -> str:
    """Human-readable one-liner, e.g. 'Relative N-min volume (2 Min) >= 200%'."""
    d = CATALOG.get(cond["id"])
    if d is None:
        return cond["id"]
    opt = ""
    if d.options and cond.get("option"):
        label = next((o.label for o in d.options if o.key == cond["option"]), cond["option"])
        opt = f" ({label})"
    if _is_bool(d):
        # "Market not against the trade >= 1" is not a sentence anyone wants.
        return f"{d.name}{opt}" + ("" if cond["value"] >= 1 else " (not required)")
    head = f"{d.name}{opt} {OP_LABEL[cond['op']]} {_amount(cond['value'], d.unit)}"
    if d.phrase:
        # Opt-in, per condition: where the params change what the number MEANS
        # ("5 of the last 5 candles on 5 min"), a description without them is
        # ambiguous. Conditions without a phrase keep their exact string, which
        # matters for saved setups and tests that compare the text exactly.
        q = cond.get("params") or {}
        try:
            vals = {pd.key: float(q.get(pd.key, pd.default)) for pd in d.params}
            txt = {k: _fmt(v) for k, v in vals.items()}
            head += " " + d.phrase.format(**txt)
            for key, tail in d.phrase_if.items():
                if vals.get(key):
                    head += tail.format(**txt)
        except (KeyError, ValueError, TypeError):
            pass
    return head


def _fmt(v: float) -> str:
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:g}M"
    if a >= 10_000:
        return f"{v / 1_000:g}K"
    return f"{v:g}"


def _amount(v: float, unit: str) -> str:
    """Render a value with its unit the way the unit is actually written."""
    n = _fmt(v)
    if unit == "$":
        return f"${n}"
    if unit in ("%", "x"):
        return f"{n}{unit}"
    if unit == "hhmm":
        t = int(round(v))
        return f"{t // 100}:{t % 100:02d}"
    return f"{n} {unit}".rstrip()


def _is_bool(d: "ConditionDef") -> bool:
    return d.unit == "bool"


def check(cond: dict, ctx: ConditionCtx) -> GateCheck:
    """Evaluate one condition. Never raises: a resolver blowing up fails closed.

    Returns a `gates.GateCheck` so profile results drop straight into the
    existing `alert["gates"]` payload and `gate_stats` counters with no new
    plumbing.
    """
    d = CATALOG.get(cond["id"])
    if d is None:
        return GateCheck(cond.get("id", "?"), False, None, "unknown condition")
    try:
        value = d.resolve(ctx, cond.get("option", ""), cond.get("params") or {})
    except Exception as exc:                      # a bad resolver must not kill the bar
        log.debug("condition %s failed for %s: %s",
                  cond["id"], getattr(ctx.state, "symbol", "?"), exc)
        value = None

    if value is None:
        if d.warming_up is not None and d.warming_up(ctx):
            return GateCheck(d.id, True, None, "warming up (pass)")
        passed = d.availability == "pass"
        why = "unavailable (pass)" if passed else "unavailable"
        return GateCheck(d.id, passed, None, why)

    ok = OPS[cond["op"]](value, cond["value"])
    if _is_bool(d):
        return GateCheck(d.id, ok, round(value, 4), "yes" if value >= 1 else "no")
    return GateCheck(d.id, ok, round(value, 4),
                     f"{_amount(value, d.unit)} {OP_LABEL[cond['op']]} "
                     f"{_amount(cond['value'], d.unit)}")


def catalog_json() -> list[dict]:
    """The whole catalog, for the dashboard's condition picker."""
    return [d.to_json() for d in CATALOG.values()]


__all__ = ["CATALOG", "ConditionCtx", "ConditionDef", "ConditionError", "OPS", "OP_LABEL",
           "catalog_json", "check", "describe", "normalize_condition"]
