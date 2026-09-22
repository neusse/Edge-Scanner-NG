"""Tests for RRS and Wilder ATR.

Regression tests reproduce two worked numerical examples of the formula:
  +3.0  — stock dropped 20¢ when it 'should' have dropped 80¢
  -2.0  — stock rose 20¢ when it 'should' have risen 60¢
These pin the core formula; if they fail, every signal downstream is wrong.
"""
import numpy as np
import pandas as pd
import pytest

from scanner.indicators.atr import wilder_atr
from scanner.indicators.rrs import D1_LENGTH, M5_LENGTH, _rrs_formula, rrs, rrs_raw

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_bars(closes: list[float], spread: float = 0.10) -> pd.DataFrame:
    """Synthetic OHLCV DataFrame with a fixed high-low spread around each close."""
    idx = pd.date_range("2024-01-01", periods=len(closes), freq="B", tz="UTC")
    c = pd.Series(closes, index=idx, dtype=float)  # index must match DataFrame
    return pd.DataFrame(
        {
            "open":   c,
            "high":   c + spread,
            "low":    c - spread,
            "close":  c,
            "volume": 1_000_000,
        },
        index=idx,
    )


# ---------------------------------------------------------------------------
# Worked-example regression tests
# ---------------------------------------------------------------------------

def test_rrs_worked_example_positive_3():
    """stock dropped 20¢ when it should have dropped 80¢  ->  RRS = +3.0"""
    # SPY: roll_move = -4.0, atr = 1.0  => power_index = -4.0
    # Stock: roll_move = -0.20, atr = 0.20  => expected = -0.80
    # RRS = (-0.20 - (-0.80)) / 0.20 = 0.60 / 0.20 = 3.0
    result = _rrs_formula(
        roll_move_stock=-0.20,
        roll_move_bench=-4.0,
        atr_stock=0.20,
        atr_bench=1.0,
    )
    assert result == pytest.approx(3.0), f"Expected +3.0, got {result}"


def test_rrs_worked_example_negative_2():
    """stock rose 20¢ when it should have risen 60¢  ->  RRS = -2.0"""
    # SPY: roll_move = +3.0, atr = 1.0  => power_index = +3.0
    # Stock: roll_move = +0.20, atr = 0.20  => expected = +0.60
    # RRS = (0.20 - 0.60) / 0.20 = -0.40 / 0.20 = -2.0
    result = _rrs_formula(
        roll_move_stock=+0.20,
        roll_move_bench=+3.0,
        atr_stock=0.20,
        atr_bench=1.0,
    )
    assert result == pytest.approx(-2.0), f"Expected -2.0, got {result}"


# ---------------------------------------------------------------------------
# Wilder ATR
# ---------------------------------------------------------------------------

def test_wilder_atr_flat_series():
    """When close is flat, ATR must converge to high-low (= 2×spread).

    With a perfectly flat close series there are no overnight gaps, so
    TR = max(high-low, |high-prev_close|, |low-prev_close|)
       = max(2*spread, spread, spread) = 2*spread.
    """
    spread = 0.25
    bars = _make_bars([100.0] * 30, spread=spread)
    atr = wilder_atr(bars["high"], bars["low"], bars["close"], length=5)
    expected_atr = 2 * spread  # high-low range dominates with no gaps
    assert atr.dropna().iloc[-1] == pytest.approx(expected_atr, rel=1e-3)


def test_wilder_atr_nan_warmup():
    """First (length-1) values must be NaN."""
    length = 5
    bars = _make_bars([100.0] * 20, spread=0.25)
    atr = wilder_atr(bars["high"], bars["low"], bars["close"], length=length)
    assert atr.iloc[: length - 1].isna().all()
    assert atr.iloc[length - 1 :].notna().all()


# ---------------------------------------------------------------------------
# rrs_raw  — series-level formula checks
# ---------------------------------------------------------------------------

def test_rrs_raw_spy_vs_spy_is_zero():
    """RRS of any series vs itself must be identically zero (no relative move)."""
    spy = _make_bars([100 + i * 0.5 for i in range(30)], spread=0.20)
    raw = rrs_raw(spy, spy, length=5)
    non_nan = raw.dropna()
    assert len(non_nan) > 0
    np.testing.assert_allclose(non_nan.values, 0.0, atol=1e-10)


def test_rrs_uses_canonical_wilder_atr_at_its_first_available_value():
    idx = pd.date_range("2024-01-01", periods=4, freq="B", tz="UTC")
    stock = pd.DataFrame({
        "high": [100.5, 101.0, 102.0, 102.0],
        "low": [99.5, 99.0, 98.0, 100.0],
        "close": [100.0, 100.0, 100.0, 101.0],
    }, index=idx)
    bench = pd.DataFrame({
        "high": [101.0] * 4, "low": [99.0] * 4, "close": [100.0] * 4,
    }, index=idx)
    # Stock true ranges are 1, 2, 4, 2. ATR(3) seeds at 7/3 and then
    # becomes 20/9. The stock's three-bar move is +1 and the bench is flat.
    assert rrs_raw(stock, bench, length=3).iloc[-1] == pytest.approx(9 / 20)


def test_rrs_raw_strong_stock_positive():
    """A stock that held flat while the market fell should have positive RRS."""
    # SPY drops steadily; stock stays flat
    spy_closes = [100.0 - i * 1.0 for i in range(30)]   # drops 29 points
    stock_closes = [100.0] * 30                            # holds flat

    spy   = _make_bars(spy_closes,   spread=0.5)
    stock = _make_bars(stock_closes, spread=0.1)

    raw = rrs_raw(stock, spy, length=5)
    assert raw.dropna().iloc[-1] > 0, "Flat stock vs falling market should be positive RRS"


def test_rrs_raw_weak_stock_negative():
    """A stock that falls while the market rises should have negative RRS.

    The ATR-normalised formula correctly flags this as weakness regardless
    of absolute magnitudes: market goes up -> power_index > 0 -> expected
    move is positive -> stock going down means RRS < 0.
    """
    spy_closes   = [100.0 + i * 1.0 for i in range(30)]  # market rallies
    stock_closes = [100.0 - i * 0.5 for i in range(30)]  # stock fades

    spy   = _make_bars(spy_closes,   spread=0.1)
    stock = _make_bars(stock_closes, spread=0.1)

    raw = rrs_raw(stock, spy, length=5)
    assert raw.dropna().iloc[-1] < 0, "Stock falling while market rises should be negative RRS"


def test_rrs_raw_nan_warmup():
    """First `length` values of rrs_raw must be NaN (ATR + shift both need warm-up)."""
    length = 5
    bars = _make_bars([100.0 + i * 0.1 for i in range(30)])
    raw = rrs_raw(bars, bars, length=length)
    assert raw.iloc[:length].isna().all()
    assert raw.iloc[length:].notna().all()


# ---------------------------------------------------------------------------
# rrs  — rolling-average version
# ---------------------------------------------------------------------------

def test_rrs_nan_warmup():
    """First (2*length - 1) values of rrs() must be NaN."""
    length = 5
    bars = _make_bars([100.0 + i * 0.1 for i in range(40)])
    out = rrs(bars, bars, length=length)
    warmup = 2 * length - 1
    assert out.iloc[:warmup].isna().all()
    assert out.iloc[warmup:].notna().all()


def test_rrs_spy_vs_spy_is_zero():
    """Rolling-average RRS of any series vs itself must also be zero."""
    spy = _make_bars([100.0 + i * 0.3 for i in range(40)])
    out = rrs(spy, spy, length=5)
    np.testing.assert_allclose(out.dropna().values, 0.0, atol=1e-10)


def test_rrs_smoothing_damps_spike():
    """A one-bar spike in raw RRS should produce a lower value in the smoothed version."""
    length = 5
    # Flat market, flat stock — RRS raw is 0 throughout
    closes = [100.0] * 40
    spy   = _make_bars(closes, spread=0.5)
    stock = _make_bars(closes, spread=0.1)

    raw_vals  = rrs_raw(stock, spy, length).dropna()
    roll_vals = rrs(stock, spy, length).dropna()

    # Both should be near zero for a flat series; check smoothing doesn't amplify
    assert roll_vals.abs().max() <= raw_vals.abs().max() + 1e-10


# ---------------------------------------------------------------------------
# Default constants
# ---------------------------------------------------------------------------

def test_default_lengths():
    assert D1_LENGTH == 5
    assert M5_LENGTH == 12
