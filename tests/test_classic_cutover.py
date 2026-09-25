"""Public calculation, catalog, trigger, and chart boundaries for Classic."""
from types import SimpleNamespace

import pandas as pd
import pytest

from scanner.api import _bars_payload
from scanner.conditions import CATALOG as CHECKS, ConditionCtx
from scanner.indicators.classic import VERSION, calculate
from scanner.trigger_catalog import BY_ID, EvalCtx, SymbolSeries, et_minutes, evaluate


def _frame(n=90):
    index = pd.date_range("2026-09-24 09:30", periods=n, freq="min",
                          tz="America/New_York")
    close = pd.Series([100 - i * .2 if i < 25 else 95 + (i - 25) * .3
                       for i in range(n)], index=index)
    return pd.DataFrame({"open": close, "high": close + .2, "low": close - .2,
                         "close": close, "volume": 1000 + pd.Series(range(n), index=index)},
                        index=index)


@pytest.mark.parametrize("study", ["sma", "ema", "macd", "adx", "rsi", "stoch",
                                     "cci", "bbands", "atr", "obv", "vwap"])
def test_every_requested_native_indicator_is_calculated_by_classic(study):
    result = calculate(_frame(), study)
    assert result.index.is_monotonic_increasing
    assert result.notna().any().all()
    assert VERSION.startswith("pandas-ta-classic/")


@pytest.mark.parametrize("study", ["ema", "macd", "rsi", "atr", "vwap"])
def test_indicator_prefix_cannot_see_later_bars(study):
    frame = _frame()
    full = calculate(frame, study).iloc[:50]
    prefix = calculate(frame.iloc[:50], study)
    pd.testing.assert_frame_equal(full, prefix)


def test_classic_checks_are_selectable_and_use_completed_candles():
    for name in ("sma", "ema", "atr", "rsi", "cci", "macd", "macd_signal",
                 "macd_hist", "stoch_k", "stoch_d", "bb_lower", "bb_middle",
                 "bb_upper", "bb_width", "bb_percent_b", "obv", "adx_plus_di", "adx_minus_di"):
        assert f"ta_{name}" in CHECKS
    series = SymbolSeries("X")
    for i, row in enumerate(_frame().itertuples()):
        t = row.Index
        bar = {"timestamp": t, "open": row.open, "high": row.high,
               "low": row.low, "close": row.close, "volume": row.volume}
        series.on_bar(bar, et_minutes(t), "2026-09-24", None)
    ctx = ConditionCtx(state=SimpleNamespace(), series=series)
    check = CHECKS["ta_rsi"]
    assert check.resolve(ctx, "", {"tf": 1, "period": 14}) == pytest.approx(
        series.study(1, "rsi", 14)["value"].iloc[-1])


def test_rsi_trigger_fires_once_on_completed_recovery_cross():
    assert "ta_rsi_cross" in BY_ID
    series = SymbolSeries("X")
    events = []
    for row in _frame(50).itertuples():
        t = row.Index
        bar = {"timestamp": t, "open": row.open, "high": row.high,
               "low": row.low, "close": row.close, "volume": row.volume}
        session = series.on_bar(bar, et_minutes(t), "2026-09-24", None)
        ctx = EvalCtx(state=SimpleNamespace(), series=series, bar=bar,
                      et_min=et_minutes(t), session=session, external=set())
        fire = evaluate("ta_rsi_cross", ctx, "up", {"tf": 1, "level": 50})
        if fire:
            events.append((t, fire))
            assert evaluate("ta_rsi_cross", ctx, "up", {"tf": 1, "level": 50}) is None
    assert len(events) == 1
    assert events[0][1].direction == "long"
    assert events[0][0].hour == 10  # no signal during initial decline/warm-up


def test_chart_overlay_values_come_from_same_classic_module():
    frame = _frame(55)
    bars = [{"t": t.isoformat(), "o": float(r.open), "h": float(r.high),
             "l": float(r.low), "c": float(r.close), "v": float(r.volume)}
            for t, r in frame.iterrows()]
    payload = _bars_payload(bars, "1min", True, False)
    assert payload["indicator_version"] == VERSION
    for key, study, period in (("ema9", "ema", 9), ("sma50", "sma", 50),
                               ("vwap", "vwap", None)):
        assert payload["indicators"][key][-1]["value"] == pytest.approx(
            calculate(frame, study, period)["value"].iloc[-1])


def test_five_minute_chart_vwap_uses_streamed_one_minute_bars():
    frame = _frame(10)
    minutes = [{"timestamp": t, "open": float(r.open), "high": float(r.high),
                "low": float(r.low), "close": float(r.close), "volume": float(r.volume)}
               for t, r in frame.iterrows()]
    bars = []
    for start in (0, 5):
        part = frame.iloc[start:start + 5]
        bars.append({"t": part.index[0].isoformat(), "o": float(part.open.iloc[0]),
                     "h": float(part.high.max()), "l": float(part.low.min()),
                     "c": float(part.close.iloc[-1]), "v": float(part.volume.sum())})
    chart = _bars_payload(bars, "5min", True, False, minute_bars=minutes)
    assert chart["indicators"]["vwap"][-1]["value"] == pytest.approx(
        calculate(frame, "vwap")["value"].iloc[-1])
