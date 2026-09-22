"""Universe conditions (scanner/conditions.py) and the shared level helpers.

Guards three things that would otherwise fail silently:
  1. the level helpers extracted from the triggers compute what the triggers
     used to compute inline, so the refactor cannot change when a trigger fires
  2. evaluating conditions never touches `series.mem`, so a condition cannot
     corrupt a trigger's edge latch
  3. rolling relative volume excludes premarket bars from its baseline
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from scanner import conditions as C
from scanner.trigger_catalog import (
    SymbolSeries,
    candle_rel_volume,
    consec_streak,
    rolling_rel_volume,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _feed(series: SymbolSeries, bars, day="2026-08-26"):
    """Push (et_min, open, close, volume) tuples through the series."""
    for et_min, o, c, v in bars:
        hi, lo = max(o, c), min(o, c)
        series.on_bar({"open": o, "high": hi, "low": lo, "close": c, "volume": v},
                      et_min, day, None)
    return series


def _rth(n, start=570, vol=1000, up=True):
    """n one-minute RTH bars from 09:30 (570)."""
    out = []
    for i in range(n):
        o = 100.0 + i
        c = o + 1 if up else o - 1
        out.append((start + i, o, c, vol))
    return out


# ── level helpers: equivalence with the old inline math ──────────────────────

def test_candle_rel_volume_matches_the_inline_math_it_replaced():
    s = _feed(SymbolSeries("T"), _rth(24))
    cs = s.last_completed(2, 6)
    assert len(cs) == 6
    expected = cs[-1]["volume"] / (sum(x["volume"] for x in cs[:-1]) / 5)
    assert candle_rel_volume(s, 2, 5) == pytest.approx(expected)


def test_candle_rel_volume_is_none_without_enough_history():
    s = _feed(SymbolSeries("T"), _rth(4))
    assert candle_rel_volume(s, 5, 20) is None


def test_candle_rel_volume_is_none_when_the_baseline_is_zero():
    s = _feed(SymbolSeries("T"), [(570 + i, 100.0, 101.0, 0.0) for i in range(24)])
    assert candle_rel_volume(s, 2, 5) is None


# ── consec_streak ────────────────────────────────────────────────────────────

def test_consec_streak_is_signed():
    s = _feed(SymbolSeries("T"), _rth(12, up=True))
    assert consec_streak(s, 1) > 0
    s2 = _feed(SymbolSeries("T"), _rth(12, up=False))
    assert consec_streak(s2, 1) < 0


def test_consec_streak_counts_only_the_current_run():
    bars = [(570, 100, 99, 1000), (571, 99, 98, 1000),       # 2 red
            (572, 98, 99, 1000), (573, 99, 100, 1000), (574, 100, 101, 1000),  # 3 green
            (575, 101, 102, 1000)]                            # completes the 3rd
    s = _feed(SymbolSeries("T"), bars)
    assert consec_streak(s, 1) == 3


def test_consec_streak_returns_zero_on_a_doji_and_none_without_history():
    s = _feed(SymbolSeries("T"), [(570, 100, 100, 10), (571, 100, 100, 10)])
    assert consec_streak(s, 1) == 0
    assert consec_streak(SymbolSeries("T"), 1) is None
    assert consec_streak(_feed(SymbolSeries("T"), _rth(4)), 99) is None


# ── the premarket contamination trap ─────────────────────────────────────────

def test_rolling_rel_volume_excludes_premarket_from_the_baseline():
    """m1 keeps premarket bars until the DAY rolls, not the session.

    Early in the session there are not yet enough RTH bars to form a baseline.
    Filtered, that is honestly reported as None ("warming up"). Unfiltered, the
    window silently reaches back into thin premarket volume and returns a large
    bogus ratio, which would pass a relative-volume screen on exactly the
    gappers it is meant to vet.
    """
    pre = [(240 + i, 100.0, 101.0, 10.0) for i in range(30)]    # 04:00, thin
    rth = [(570 + i, 100.0, 101.0, 1000.0) for i in range(6)]   # 09:30-09:35 only
    s = _feed(SymbolSeries("T"), pre + rth)

    filtered = rolling_rel_volume(s, 2, 10, session="rth")
    unfiltered = rolling_rel_volume(s, 2, 10, session=None)

    assert filtered is None, "6 RTH bars cannot fill a 22-bar window"
    # The unfiltered window is 22 bars: 6 RTH plus 16 premarket. Its baseline
    # averages eight thin premarket blocks against two RTH ones, so it reads
    # ~4.8x where the honest answer is 1.0x. That clears a "200%" screen.
    assert unfiltered is not None and unfiltered > 3.0, (
        "unfiltered reaches into premarket and invents a baseline")


def test_rolling_rel_volume_reads_flat_rth_volume_as_one():
    rth = [(570 + i, 100.0, 101.0, 1000.0) for i in range(30)]
    s = _feed(SymbolSeries("T"), [(240 + i, 100.0, 101.0, 10.0) for i in range(30)] + rth)
    assert rolling_rel_volume(s, 2, 10, session="rth") == pytest.approx(1.0)


def test_rolling_rel_volume_detects_a_real_surge():
    bars = [(570 + i, 100.0, 101.0, 1000.0) for i in range(20)]
    bars += [(590, 100.0, 101.0, 5000.0), (591, 100.0, 101.0, 5000.0)]
    s = _feed(SymbolSeries("T"), bars)
    assert rolling_rel_volume(s, 2, 10, session="rth") == pytest.approx(5.0)


def test_rolling_rel_volume_is_none_before_the_window_fills():
    s = _feed(SymbolSeries("T"), _rth(5))
    assert rolling_rel_volume(s, 2, 10, session="rth") is None


# ── the edge-vs-level invariant ──────────────────────────────────────────────

def test_conditions_never_touch_the_trigger_edge_memory():
    """A condition must not be able to corrupt a trigger's latch."""
    s = _feed(SymbolSeries("T"), _rth(40))
    s.mem["consec:green:2"] = True
    s.mem["rvol_cross"] = True
    before = dict(s.mem)

    state = SimpleNamespace(symbol="T", dist_vwap_pct=1.0, dist_ema9_pct=0.5,
                            rvol=1.5, session_volume=1e6, gap_pct=2.0)
    ctx = C.ConditionCtx(state=state, series=s, bar={"close": 140.0}, session="rth")
    for cid in ("rel_vol", "consec_candles", "dist_vwap_pct", "dist_ema9_pct",
                "rvol", "session_volume", "gap_pct"):
        C.check(C.normalize_condition({"id": cid}), ctx)

    assert s.mem == before


def test_condition_ctx_has_no_edge_or_once():
    """Structural guarantee, not a convention."""
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="T"))
    assert not hasattr(ctx, "edge")
    assert not hasattr(ctx, "once")


# ── normalize ────────────────────────────────────────────────────────────────

def test_normalize_rejects_unknown_conditions_and_operators():
    with pytest.raises(C.ConditionError):
        C.normalize_condition({"id": "no_such_condition"})
    with pytest.raises(C.ConditionError):
        C.normalize_condition({"id": "price", "op": "between"})
    with pytest.raises(C.ConditionError):
        C.normalize_condition({"id": "rel_vol", "option": "7"})
    with pytest.raises(C.ConditionError):
        C.normalize_condition({"id": "price", "value": "cheap"})


def test_normalize_clamps_values_and_params_to_their_range():
    c = C.normalize_condition({"id": "atr_pct", "value": 1e9})
    assert c["value"] == 100.0
    c = C.normalize_condition({"id": "rel_vol", "params": {"lookback": 9999}})
    assert c["params"]["lookback"] == 60
    c = C.normalize_condition({"id": "rel_vol", "params": {"lookback": "junk"}})
    assert c["params"]["lookback"] == 10


def test_normalize_fills_defaults():
    c = C.normalize_condition({"id": "rvol"})
    assert c == {"id": "rvol", "op": "gte", "value": 1.0, "option": "", "params": {}}


# ── evaluation ───────────────────────────────────────────────────────────────

def _state(**kw):
    base = dict(symbol="T", adv20=2_000_000.0, prior_close=50.0, atr_d1=1.0,
                chart_quality=70.0, rvol=1.4, session_volume=900_000.0,
                dist_vwap_pct=-3.0, dist_ema9_pct=1.0, gap_pct=2.5,
                day_range_pos=0.4, rth_chg_pct=3.0, mom_15m_pct=0.8,
                _last_close=50.0)
    base.update(kw)
    return SimpleNamespace(**base)


def test_static_conditions_resolve_from_symbol_state():
    ctx = C.ConditionCtx(state=_state(), bar={"close": 50.0})
    assert C.check(C.normalize_condition(
        {"id": "avg_dollar_vol_20d", "value": 50_000_000}), ctx).passed is True
    assert C.check(C.normalize_condition(
        {"id": "avg_dollar_vol_20d", "value": 200_000_000}), ctx).passed is False
    # ATR% = atr_d1 / prior_close * 100 = 1.0 / 50 * 100 = 2%
    assert C.check(C.normalize_condition({"id": "atr_pct", "value": 1.0}), ctx).value == 2.0


def test_dist_vwap_options_are_signed_correctly():
    ctx = C.ConditionCtx(state=_state(dist_vwap_pct=-3.0), bar={"close": 50.0})
    below = C.check(C.normalize_condition(
        {"id": "dist_vwap_pct", "option": "below", "value": 2.0}), ctx)
    above = C.check(C.normalize_condition(
        {"id": "dist_vwap_pct", "option": "above", "value": 2.0}), ctx)
    either = C.check(C.normalize_condition(
        {"id": "dist_vwap_pct", "option": "abs", "value": 2.0}), ctx)
    assert below.passed is True and below.value == 3.0
    assert above.passed is False
    assert either.passed is True and either.value == 3.0


def test_missing_values_fail_closed_except_fundamentals():
    ctx = C.ConditionCtx(state=_state(adv20=None, rvol=None), bar={"close": 50.0},
                         fundamentals=None)
    blocked = C.check(C.normalize_condition({"id": "avg_vol_20d"}), ctx)
    assert blocked.passed is False and blocked.reason == "unavailable"

    passed = C.check(C.normalize_condition({"id": "float_shares"}), ctx)
    assert passed.passed is True and "pass" in passed.reason

    assert C.CATALOG["market_cap"].availability == "pass"
    assert C.CATALOG["price"].availability == "block"


def test_a_resolver_that_raises_fails_closed_without_propagating():
    class Boom:
        symbol = "T"

        def __getattr__(self, k):
            raise RuntimeError("boom")

    ctx = C.ConditionCtx(state=Boom())
    got = C.check(C.normalize_condition({"id": "rvol"}), ctx)
    assert got.passed is False


def test_check_returns_a_gatecheck_the_alert_payload_already_understands():
    from scanner.gates import GateCheck
    ctx = C.ConditionCtx(state=_state(), bar={"close": 50.0})
    got = C.check(C.normalize_condition({"id": "rvol", "value": 1.0}), ctx)
    assert isinstance(got, GateCheck)
    assert got.name == "rvol" and got.passed is True


def test_opening_direction_must_agree_with_the_candidate_trade():
    bullish = _state(opening_candle_direction=1.0)
    cond = C.normalize_condition({"id": "opening_direction_m5", "option": "trade"})
    assert C.check(cond, C.ConditionCtx(state=bullish, direction="long")).passed is True
    assert C.check(cond, C.ConditionCtx(state=bullish, direction="short")).passed is False
    doji = _state(opening_candle_direction=0.0)
    assert C.check(cond, C.ConditionCtx(state=doji, direction="long")).passed is False


def test_opening_rvol_and_rank_conditions_fail_closed_until_rank_is_ready():
    state = _state(opening_rvol_m5=2.5, opening_rvol_rank=7.0,
                   opening_rvol_coverage=0.79)
    rvol = C.normalize_condition({"id": "opening_rvol_m5", "value": 1.0})
    rank = C.normalize_condition({"id": "opening_rvol_rank", "value": 20,
                                  "params": {"min_coverage": 80}})
    assert C.check(rvol, C.ConditionCtx(state=state)).passed is True
    blocked = C.check(rank, C.ConditionCtx(state=state))
    assert blocked.passed is False and blocked.reason == "unavailable"
    state.opening_rvol_coverage = 0.8
    ready = C.check(rank, C.ConditionCtx(state=state))
    assert ready.passed is True and ready.value == 7.0


def test_opening_rank_uses_lower_is_better_semantics():
    cond = C.normalize_condition({"id": "opening_rvol_rank", "value": 20})
    top = _state(opening_rvol_rank=20.0, opening_rvol_coverage=1.0)
    outside = _state(opening_rvol_rank=21.0, opening_rvol_coverage=1.0)
    assert C.check(cond, C.ConditionCtx(state=top)).passed is True
    assert C.check(cond, C.ConditionCtx(state=outside)).passed is False


def test_describe_reads_like_a_plain_scanner_filter():
    assert C.describe(C.normalize_condition(
        {"id": "rel_vol", "option": "2", "value": 200})) == "Relative N-min volume (2 Min) >= 200%"
    assert C.describe(C.normalize_condition(
        {"id": "price", "value": 10})) == "Last price >= $10"
    assert C.describe(C.normalize_condition(
        {"id": "session_volume", "value": 500_000})) == "Volume today >= 500K shares"


def test_every_catalog_entry_is_serialisable_and_well_formed():
    import json
    js = C.catalog_json()
    json.dumps(js)
    assert len(js) == len(C.CATALOG)
    for d in C.CATALOG.values():
        assert d.kind in ("static", "dynamic")
        assert d.availability in ("block", "pass")
        assert d.default_op in d.ops
        assert all(o in C.OPS for o in d.ops)
        if d.options:
            assert d.default_option in {o.key for o in d.options}


# ── the shared gate vocabulary ───────────────────────────────────────────────
#
# Each of these must give the SAME answer as the gate in scanner/gates.py it
# mirrors. That equivalence is what lets any setup apply the shared gates as
# ordinary conditions, so it is asserted against the gate itself rather than
# against a number typed in here.

import pandas as pd

from scanner.gates import gate_market_align, gate_void
from scanner.market import MarketRegime


def _bar(hh=10, mm=30, close=100.0):
    ts = pd.Timestamp(f"2026-08-26 {hh:02d}:{mm:02d}", tz="America/New_York").tz_convert("UTC")
    return {"timestamp": ts, "open": close, "high": close, "low": close,
            "close": close, "volume": 1000}


def _cond(cid, value, op="gte", option="", **params):
    return {"id": cid, "op": op, "value": value, "option": option, "params": params}


# -- relative strength vs SPY --

@pytest.mark.parametrize("rrs,direction,expected", [
    (0.5, "long", True), (0.5, "short", False),
    (-0.5, "long", False), (-0.5, "short", True),
])
def test_rrs_in_the_trade_direction_matches_the_gate(rrs, direction, expected):
    st = SimpleNamespace(symbol="AAA", rrs_m5=rrs)
    ctx = C.ConditionCtx(state=st, bar=_bar(), direction=direction)
    assert C.check(_cond("rrs_m5", 0.0, op="gt", option="trade"), ctx).passed is expected


def test_rrs_raw_is_not_signed_by_direction():
    st = SimpleNamespace(symbol="AAA", rrs_m5=-0.5)
    for d in ("long", "short"):
        ctx = C.ConditionCtx(state=st, bar=_bar(), direction=d)
        assert C.check(_cond("rrs_m5", 0.0, op="lt", option="raw"), ctx).passed is True


def test_rrs_reads_m5_not_d1():
    """gate_rrs_d1 gates on m5 and keeps d1 for display. Reading d1 here would
    silently gate on a different number."""
    st = SimpleNamespace(symbol="AAA", rrs_m5=-1.0, rrs_d1=9.0)
    ctx = C.ConditionCtx(state=st, bar=_bar(), direction="long")
    assert C.check(_cond("rrs_m5", 0.0, op="gt", option="trade"), ctx).passed is False


def test_rrs_unavailable_fails_closed():
    st = SimpleNamespace(symbol="AAA", rrs_m5=None)
    ctx = C.ConditionCtx(state=st, bar=_bar(), direction="long")
    got = C.check(_cond("rrs_m5", 0.0, op="gt", option="trade"), ctx)
    assert got.passed is False and got.reason == "unavailable"


@pytest.mark.parametrize("n_bars", [0, 1, 11, 12, 20])
def test_rrs_missing_matches_the_gate_through_warmup(n_bars):
    """Before about 10:30 RRS does not exist yet. The gate passes then and
    blocks after; a condition blocking from the open would keep any setup
    using it silent for the first hour, which is all of an opening-range window."""
    from scanner.gates import gate_rrs_d1
    st = SimpleNamespace(symbol="AAA", rrs_m5=None, _stock_5m=[{}] * n_bars)
    ctx = C.ConditionCtx(state=st, bar=_bar(), direction="long")
    got = C.check(_cond("rrs_m5", 0.0, op="gt", option="trade"), ctx)
    gate = gate_rrs_d1(0.0, None, "long", bars_5m_count=n_bars)
    assert got.passed is (gate.passed is not False)
    assert got.passed is (n_bars < 12)


# -- clear air --

@pytest.mark.parametrize("direction", ["long", "short"])
def test_void_matches_gate_void(direction):
    highs = pd.Series([104.0, 110.0, 130.0])
    lows = pd.Series([96.0, 90.0, 70.0])
    st = SimpleNamespace(symbol="AAA", daily_highs_60d=highs, daily_lows_60d=lows, _last_close=100.0)
    ctx = C.ConditionCtx(state=st, bar=_bar(close=100.0), direction=direction)
    got = C.check(_cond("void_pct", 1.0), ctx)
    assert got.value == pytest.approx(gate_void(100.0, highs, lows, direction).value)


def test_void_is_unbounded_when_no_level_is_in_the_way():
    st = SimpleNamespace(symbol="AAA", daily_highs_60d=pd.Series([50.0]),
                         daily_lows_60d=pd.Series([10.0]), _last_close=100.0)
    ctx = C.ConditionCtx(state=st, bar=_bar(close=100.0), direction="long")
    assert C.check(_cond("void_pct", 1.0), ctx).value == 999.0


# -- market alignment --

@pytest.mark.parametrize("regime,direction,expected", [
    (MarketRegime.NEUTRAL, "long", True),    # neutral allows BOTH
    (MarketRegime.NEUTRAL, "short", True),
    (MarketRegime.BEARISH, "long", False),
    (MarketRegime.BEARISH, "short", True),
    (MarketRegime.BULLISH, "long", True),
    (MarketRegime.BULLISH, "short", False),
])
def test_market_align_matches_the_gate(regime, direction, expected):
    bar = _bar(11, 0)
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="AAA"), bar=bar,
                         direction=direction, regime=regime)
    assert C.check(_cond("market_align", 1.0, **{"from": 600}), ctx).passed is expected
    assert gate_market_align(pd.Timestamp(bar["timestamp"]), regime, direction).passed is expected


def test_market_align_is_off_before_the_cutoff():
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="AAA"), bar=_bar(9, 45),
                         direction="long", regime=MarketRegime.BEARISH)
    assert C.check(_cond("market_align", 1.0, **{"from": 600}), ctx).passed is True


def test_market_align_reads_as_yes_or_no_not_as_a_number():
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="AAA"), bar=_bar(11, 0),
                         direction="long", regime=MarketRegime.BEARISH)
    assert C.check(_cond("market_align", 1.0, **{"from": 600}), ctx).reason == "no"
    assert C.describe(_cond("market_align", 1.0)) == "Market not against the trade"


def test_market_align_without_a_regime_fails_closed():
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="AAA"), bar=_bar(11, 0), direction="long")
    assert C.check(_cond("market_align", 1.0), ctx).passed is False


# -- VWAP side --

@pytest.mark.parametrize("dist,direction,expected", [
    (0.5, "long", True), (0.5, "short", False),
    (-0.5, "long", False), (-0.5, "short", True),
])
def test_vwap_in_the_trade_direction(dist, direction, expected):
    price = 100.0
    st = SimpleNamespace(symbol="AAA", dist_vwap_pct=dist)
    ctx = C.ConditionCtx(state=st, bar=_bar(close=price), direction=direction)
    assert C.check(_cond("dist_vwap_pct", 0.0, option="trade"), ctx).passed is expected


def test_void_reads_the_state_attribute_that_actually_exists():
    """SymbolState calls them daily_highs_60d / daily_lows_60d. Reading the
    shorter name returns None, fails closed, and silently blocks every alert
    that uses this condition. A replay comparison caught exactly that."""
    from scanner.state import SymbolState
    import inspect
    sig = inspect.signature(SymbolState.__init__).parameters
    assert "daily_highs_60d" in sig and "daily_lows_60d" in sig
    assert "daily_highs" not in sig and "daily_lows" not in sig


# ── VWAP holding: support that is actually support ───────────────────────────

def _series_5m(sides, vwap=100.0, start=570):
    """Five-minute candles whose closes sit above (+1) or below (-1) VWAP.

    Each candle is five 1-min bars, all passed the same VWAP, so the candle's
    recorded VWAP is `vwap`. One extra bar opens the next candle so the last of
    `sides` is COMPLETED rather than still forming.
    """
    s = SymbolSeries("X")
    m = start
    for side in sides:
        px = vwap + 0.5 * side
        for _ in range(5):
            s.on_bar({"open": px, "high": px + 0.1, "low": px - 0.1, "close": px, "volume": 1000},
                     m, "2026-09-11", vwap)
            m += 1
    s.on_bar({"open": vwap, "high": vwap, "low": vwap, "close": vwap, "volume": 1000},
             m, "2026-09-11", vwap)
    return s


def _hold(n, op="gte", option="trade", lookback=5, tf=5):
    return {"id": "vwap_hold", "op": op, "value": n, "option": option,
            "params": {"lookback": lookback, "tf": tf}}


def test_five_of_five_above_vwap_passes_for_a_long():
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="X"), series=_series_5m([1] * 5),
                         direction="long")
    got = C.check(_hold(5), ctx)
    assert got.passed and got.value == 5


def test_one_candle_across_vwap_breaks_a_five_of_five_run():
    """The whole point: price that keeps crossing VWAP is not treating it as support."""
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="X"), series=_series_5m([1, 1, -1, 1, 1]),
                         direction="long")
    assert C.check(_hold(5), ctx).passed is False
    assert C.check(_hold(4), ctx).passed is True       # the common 4-of-5 rule


def test_a_short_counts_candles_below_vwap():
    s = _series_5m([-1] * 5)
    short = C.ConditionCtx(state=SimpleNamespace(symbol="X"), series=s, direction="short")
    long_ = C.ConditionCtx(state=SimpleNamespace(symbol="X"), series=s, direction="long")
    assert C.check(_hold(5), short).passed is True
    assert C.check(_hold(5), long_).passed is False


def test_it_cannot_pass_before_the_session_has_n_candles():
    """Fails closed until there has been time for VWAP to hold: 09:55 on 5 x 5-min."""
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="X"), series=_series_5m([1] * 3),
                         direction="long")
    got = C.check(_hold(3, lookback=5), ctx)
    assert got.passed is False and got.reason == "unavailable"


def test_history_candles_without_a_vwap_do_not_count():
    """Seeded and premarket candles carry no VWAP; VWAP resets each session."""
    s = _series_5m([1] * 5)
    s.candles[5][0]["vwap"] = None            # as a seeded history candle would be
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="X"), series=s, direction="long")
    assert C.check(_hold(5), ctx).passed is False


def test_vwap_hold_describes_its_lookback_and_timeframe():
    assert C.describe(_hold(5)) == \
        "Held above / below VWAP (In the trade direction) >= 5 of the last 5 candles on 5 min"


# -- Trend watchlist conditions: time of day, VWAP all day, held above an EMA --

def _day_candles(closes_vwaps, day="2026-09-15", start=570):
    """One 5-minute candle per (close, vwap), built from 1-min bars, then one bar
    into the next candle so the last listed candle is completed."""
    from scanner.trigger_catalog import SymbolSeries
    s = SymbolSeries("X")
    m = start
    for c, v in closes_vwaps:
        for _ in range(5):
            s.on_bar({"open": c, "high": c, "low": c, "close": c, "volume": 1}, m, day, v)
            m += 1
    last = closes_vwaps[-1]
    s.on_bar({"open": last[0], "high": last[0], "low": last[0], "close": last[0], "volume": 1}, m, day, last[1])
    return s


def test_time_of_day_uses_the_bar_close_in_et():
    import pandas as pd
    bar = {"timestamp": pd.Timestamp("2026-09-15 10:29", tz="America/New_York"), "close": 1.0}
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="X"), bar=bar)
    got = C.check({"id": "time_et", "op": "gte", "value": 1030.0, "option": "", "params": {}}, ctx)
    assert got.passed is True and got.value == 1030.0          # the 10:29 bar closes at 10:30
    early = C.check({"id": "time_et", "op": "gte", "value": 1031.0, "option": "", "params": {}}, ctx)
    assert early.passed is False and "10:30" in early.reason


def test_vwap_all_day_is_a_percent_of_todays_candles():
    s = _day_candles([(101, 100), (102, 100), (99, 100), (103, 100)])      # 3 of 4 above
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="X"), series=s, direction="long")
    cond = {"id": "vwap_day_pct", "op": "gte", "value": 100.0, "option": "trade", "params": {"tf": 5.0}}
    got = C.check(cond, ctx)
    assert got.value == 75.0 and got.passed is False
    short = C.ConditionCtx(state=SimpleNamespace(symbol="X"), series=s, direction="short")
    assert C.check(cond, short).value == 25.0


def test_held_above_ema_counts_each_candle_against_its_own_ema():
    rising = [(100 + i, 50) for i in range(12)]
    s = _day_candles(rising)
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="X"), series=s, direction="long")
    cond = {"id": "ema_hold", "op": "gte", "value": 3.0, "option": "trade",
            "params": {"period": 9.0, "lookback": 3.0, "tf": 5.0}}
    assert C.check(cond, ctx).passed is True
    falling = _day_candles([(100 + i, 50) for i in range(10)] + [(95, 50), (94, 50), (93, 50)])
    ctx2 = C.ConditionCtx(state=SimpleNamespace(symbol="X"), series=falling, direction="long")
    assert C.check(cond, ctx2).value == 0.0


def _series_with_5m_closes(closes):
    s = SymbolSeries("X")
    for i, close in enumerate(closes):
        s.candles[5].append({
            "key": ("2026-09-15", "rth", i),
            "open": close - 0.25,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": 1000.0,
            "vwap": 100.0,
            "et_min": 570 + i * 5,
            "session": "rth",
        })
    return s


def test_adx_condition_passes_a_strong_trend_and_exposes_its_parameters():
    s = _series_with_5m_closes([100.0 + i for i in range(40)])
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="X"), series=s)
    cond = C.normalize_condition({"id": "adx", "value": 30, "params": {"period": 14, "tf": 5}})
    got = C.check(cond, ctx)
    assert got.passed is True and got.value == pytest.approx(100.0)
    assert C.describe(cond) == "ADX >= 30 on 5 min, period 14"


def test_adx_condition_fails_closed_until_the_full_wilder_warmup():
    s = _series_with_5m_closes([100.0 + i for i in range(27)])
    ctx = C.ConditionCtx(state=SimpleNamespace(symbol="X"), series=s)
    got = C.check(C.normalize_condition({"id": "adx"}), ctx)
    assert got.passed is False and got.reason == "unavailable"


def test_daily_atr_extension_is_signed_by_side_and_trade_direction():
    state = SimpleNamespace(symbol="X", _last_close=106.0, prior_close=100.0,
                            ema_8_d1=100.0, atr_14_d1=2.0)
    long_ctx = C.ConditionCtx(state=state, bar={"close": 106.0}, direction="long")
    short_ctx = C.ConditionCtx(state=state, bar={"close": 106.0}, direction="short")
    base = {"id": "atr_extension_d1", "op": "gte", "value": 2.5}
    assert _val(dict(base, option="trade"), long_ctx).value == 3.0
    assert _val(dict(base, option="trade"), short_ctx).value == -3.0
    assert _val(dict(base, option="above"), short_ctx).value == 3.0
    assert C.describe(C.normalize_condition(dict(base, option="above"))) == \
        "Daily ATR extension (Above EMA8) >= 2.5 x ATR"


def test_intraday_sector_rrs_is_signed_by_trade_direction():
    cond = {"id": "rrs_sector_m5", "op": "gte", "value": 1.0}
    assert _val(cond, _ctx("long", rrs_sector_m5=1.5)).passed
    assert not _val(cond, _ctx("short", rrs_sector_m5=1.5)).passed
    assert _val(cond, _ctx("short", rrs_sector_m5=-1.5)).passed


def test_vwap_hold_margin_rejects_a_stock_parked_on_vwap():
    """META 2026-09-16: every 5-min close a few cents above VWAP passed 4 of 5.
    With a margin, a candle only counts when it closed clearly above."""
    s = _day_candles([(100.02, 100), (100.03, 100), (99.99, 100), (100.02, 100), (100.01, 100)])
    st = SimpleNamespace(symbol="X", atr_d1=2.0)
    ctx = C.ConditionCtx(state=st, series=s, direction="long")
    base = {"id": "vwap_hold", "op": "gte", "value": 4.0, "option": "trade"}
    assert C.check(dict(base, params={"lookback": 5.0, "tf": 5.0, "margin": 0.0}), ctx).passed is True
    assert C.check(dict(base, params={"lookback": 5.0, "tf": 5.0, "margin": 5.0}), ctx).passed is False
    clear = _day_candles([(100.3, 100), (100.4, 100), (100.2, 100), (100.5, 100), (100.3, 100)])
    ctx2 = C.ConditionCtx(state=st, series=clear, direction="long")
    assert C.check(dict(base, params={"lookback": 5.0, "tf": 5.0, "margin": 5.0}), ctx2).passed is True


# ── setup-library conditions: daily RS, RS slope, distance from a level ──────

def _ctx(direction=None, **state):
    return C.ConditionCtx(state=SimpleNamespace(symbol="T", **state), series=None, direction=direction)


def _val(cond, ctx):
    return C.check(C.normalize_condition(cond), ctx)


def test_rrs_d1_is_signed_by_the_trade_direction():
    cond = {"id": "rrs_d1", "op": "gte", "value": 1.0}
    assert _val(cond, _ctx("long", rrs_d1=1.5)).passed
    assert not _val(cond, _ctx("short", rrs_d1=1.5)).passed
    assert _val(cond, _ctx("short", rrs_d1=-1.5)).passed      # a daily laggard, for a short
    assert _val(dict(cond, option="raw", op="lte", value=-1.0), _ctx("long", rrs_d1=-1.5)).passed


def test_rrs_slope_is_the_change_over_n_bars():
    tail = [0.1, 0.2, 0.4, 0.9]
    cond = {"id": "rrs_slope", "op": "gte", "value": 0.0, "option": "raw", "params": {"bars": 3}}
    assert _val(cond, _ctx(rrs_m5_tail=tail)).value == pytest.approx(0.8)
    assert _val(dict(cond, params={"bars": 1}), _ctx(rrs_m5_tail=tail)).value == pytest.approx(0.5)
    # Falling strength is what a short wants: signed, it reads positive.
    assert _val(dict(cond, option="trade"), _ctx("short", rrs_m5_tail=tail[::-1])).value == pytest.approx(0.8)


def test_rrs_slope_blocks_without_history_unless_rs_is_still_warming_up():
    cond = {"id": "rrs_slope", "op": "gte", "value": 0.3}
    assert not _val(cond, _ctx(rrs_m5_tail=[0.5])).passed                         # unknown: block
    assert _val(cond, _ctx(rrs_m5_tail=[], _stock_5m=[{}] * 3)).passed            # before ~10:30: pass


def test_dist_level_sides_and_units():
    st = dict(_last_close=101.0, prior_close=99.0, prior_high=100.0, atr_d1=2.0)
    near = {"id": "dist_level", "op": "lte", "value": 1.5, "option": "prior_high"}
    assert _val(near, _ctx(**st)).value == pytest.approx(1.0)                     # either side, % of price
    above = dict(near, op="gte", value=0, params={"side": 1})
    below = dict(near, op="gte", value=0, params={"side": -1})
    assert _val(above, _ctx(**st)).passed and not _val(below, _ctx(**st)).passed
    assert _val(dict(near, params={"unit": 1}), _ctx(**st)).value == pytest.approx(50.0)   # $1 of a $2 ATR


def test_dist_level_is_unavailable_when_the_level_is():
    cond = {"id": "dist_level", "op": "lte", "value": 1, "option": "ema9_15"}     # needs a series
    got = _val(cond, _ctx(_last_close=101.0, prior_close=99.0))
    assert not got.passed and got.value is None


def test_level_value_is_what_the_cross_triggers_resolve():
    from scanner.trigger_catalog import level_value
    st = SimpleNamespace(prior_high=100.0, prior_low=90.0, prior_close=95.0, vwap=97.0,
                         sma_50=80.0, sma_100=70.0, sma_200=60.0, pm_high=99.0, pm_low=94.0)
    s = _feed(SymbolSeries("T"), _rth(3))
    want = {"prior_high": 100.0, "prior_low": 90.0, "prior_close": 95.0, "vwap": 97.0,
            "sma50_d": 80.0, "sma100_d": 70.0, "sma200_d": 60.0, "open": 100.0}
    assert {k: level_value(s, st, k) for k in want} == want
    assert level_value(None, st, "pm_high") == 99.0          # falls back to the state without a series


def test_state_keeps_the_rrs_tail_it_already_computes():
    from tests.helpers import _feed as feed_state, _state
    st = _state()
    assert st.rrs_m5_tail == []
    feed_state(st, [100.0 + i * 0.1 for i in range(30)], start="09:30")
    assert isinstance(st.rrs_m5_tail, list)
