"""Unit tests for EMA/SMA, VWAP, RVOL, and chart quality indicators."""
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scanner.indicators.ema_sma import ema, ema_update, sma
from scanner.indicators.vwap import session_vwap, vwap_update
from scanner.indicators.rvol import build_volume_profile, compute_rvol
from scanner.indicators.chart_quality import chart_quality
from scanner.indicators.adx import adx
from scanner.indicators.atr import wilder_atr


# ── Helpers ───────────────────────────────────────────────────────────────────

def _daily_index(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")


def _intraday_5m_index(n_days: int = 1) -> pd.DatetimeIndex:
    """5-min bars for n_days of regular session (9:30–16:00 ET = 78 bars/day)."""
    slots = []
    for d in range(n_days):
        base = pd.Timestamp("2024-01-02", tz="America/New_York") + pd.Timedelta(days=d)
        # skip weekends
        if base.dayofweek >= 5:
            continue
        for minute in range(0, 6 * 60 + 30, 5):
            slots.append(
                base + pd.Timedelta(hours=9, minutes=30) + pd.Timedelta(minutes=minute)
            )
    return pd.DatetimeIndex(slots).tz_convert("UTC")


# ── EMA / SMA ─────────────────────────────────────────────────────────────────

def test_sma_flat_series():
    idx = _daily_index(20)
    s = pd.Series([10.0] * 20, index=idx)
    out = sma(s, 5)
    assert out.dropna().iloc[-1] == pytest.approx(10.0)


def test_sma_nan_warmup():
    idx = _daily_index(10)
    s = pd.Series([float(i) for i in range(10)], index=idx)
    out = sma(s, 5)
    assert out.iloc[:4].isna().all()
    assert out.iloc[4:].notna().all()


def test_ema_converges_to_flat():
    idx = _daily_index(50)
    s = pd.Series([100.0] * 50, index=idx)
    out = ema(s, 8)
    assert out.iloc[-1] == pytest.approx(100.0, rel=1e-6)


def test_daily_ema_uses_the_same_sma_seed_as_alert_emas():
    close = pd.Series([1.0, 2.0, 3.0, 4.0], index=_daily_index(4))
    out = ema(close, 3)
    assert out.iloc[:2].isna().all()
    assert out.iloc[2:].tolist() == pytest.approx([2.0, 3.0])


@pytest.mark.parametrize("period", [9, 21])
def test_ema_matches_shared_chart_golden_fixture(period):
    fixture = json.loads((Path(__file__).parent / "fixtures" / "ema_contract.json").read_text())
    closes = pd.Series(fixture["closes"], index=_daily_index(len(fixture["closes"])))
    expected = fixture[f"ema{period}"]
    out = ema(closes, period)
    assert out.iloc[:expected["first_index"]].isna().all()
    assert out.iloc[expected["first_index"]] == pytest.approx(expected["first_value"])
    assert out.iloc[-1] == pytest.approx(expected["last_value"])


def test_ema_update_seeds_on_first_bar():
    result = ema_update(None, 42.0, span=8)
    assert result == 42.0


def test_ema_update_incremental():
    alpha = 2.0 / (8 + 1)
    prev = 100.0
    price = 110.0
    expected = alpha * price + (1 - alpha) * prev
    assert ema_update(prev, price, span=8) == pytest.approx(expected)


# ── Session VWAP ──────────────────────────────────────────────────────────────

def test_session_vwap_uniform_price():
    """When price is constant, VWAP must equal that price."""
    n = 10
    idx = pd.date_range("2024-01-02 14:30", periods=n, freq="1min", tz="UTC")
    price = 150.0
    s = pd.Series([price] * n, index=idx)
    vol = pd.Series([1_000.0] * n, index=idx)
    vwap = session_vwap(s, s, s, vol)
    assert vwap.iloc[-1] == pytest.approx(price)


def test_session_vwap_weighted():
    """VWAP should weight higher-volume bars more."""
    idx = pd.date_range("2024-01-02 14:30", periods=2, freq="1min", tz="UTC")
    high  = pd.Series([100.0, 200.0], index=idx)
    low   = pd.Series([100.0, 200.0], index=idx)
    close = pd.Series([100.0, 200.0], index=idx)  # tp = same as close
    volume = pd.Series([1.0, 3.0], index=idx)
    # tp bar0=100, tp bar1=200; weighted = (100*1 + 200*3)/(1+3) = 700/4 = 175
    vwap = session_vwap(high, low, close, volume)
    assert vwap.iloc[-1] == pytest.approx(175.0)


def test_vwap_update_incremental():
    num, den, v = vwap_update(0.0, 0.0, high=105.0, low=95.0, close=100.0, volume=1000.0)
    assert den == pytest.approx(1000.0)
    assert v == pytest.approx(100.0)  # tp = (105+95+100)/3 = 100
    # Add a second bar at same price
    num2, den2, v2 = vwap_update(num, den, 105.0, 95.0, 100.0, 2000.0)
    assert v2 == pytest.approx(100.0)  # uniform price -> same VWAP


# ── RVOL ──────────────────────────────────────────────────────────────────────

def _make_5m_history(n_days: int = 20, volume_per_slot: float = 100_000.0) -> pd.DataFrame:
    """Synthetic 5-min history with uniform volume per slot."""
    idx = _intraday_5m_index(n_days=n_days)
    return pd.DataFrame(
        {
            "open": 100.0, "high": 101.0, "low": 99.0,
            "close": 100.0, "volume": volume_per_slot,
        },
        index=idx,
    )


def test_build_volume_profile_slot_count():
    """Regular session = 390 min / 5 = 78 slots (0, 5, ..., 385)."""
    df = _make_5m_history(n_days=5)
    profile = build_volume_profile(df)
    assert len(profile) == 78
    assert profile.index[0] == 0    # 9:30 ET
    assert profile.index[-1] == 385  # 15:55 ET


def test_build_volume_profile_uniform_volume():
    """With constant per-slot volume, profile must equal that volume everywhere."""
    vol = 50_000.0
    df = _make_5m_history(n_days=10, volume_per_slot=vol)
    profile = build_volume_profile(df)
    np.testing.assert_allclose(profile.values, vol, rtol=1e-6)


def test_compute_rvol_at_expected_pace():
    """When actual volume equals expected, RVOL = 1.0."""
    vol_per_slot = 100_000.0
    df = _make_5m_history(n_days=20, volume_per_slot=vol_per_slot)
    profile = build_volume_profile(df)
    # Simulate 6 slots elapsed (30 minutes into session)
    elapsed_slots = 6
    cum_vol = vol_per_slot * elapsed_slots
    rvol = compute_rvol(profile, cum_vol, minutes_from_open=elapsed_slots * 5)
    assert rvol == pytest.approx(1.0, rel=1e-3)


def test_compute_rvol_double_volume():
    """Volume running at 2x pace -> RVOL = 2.0."""
    vol_per_slot = 100_000.0
    df = _make_5m_history(n_days=20, volume_per_slot=vol_per_slot)
    profile = build_volume_profile(df)
    elapsed_slots = 10
    cum_vol = vol_per_slot * elapsed_slots * 2.0
    rvol = compute_rvol(profile, cum_vol, minutes_from_open=elapsed_slots * 5)
    assert rvol == pytest.approx(2.0, rel=1e-3)


def test_compute_rvol_empty_profile():
    assert compute_rvol(pd.Series(dtype=float), 1_000.0, 30) is float("nan") or \
        pd.isna(compute_rvol(pd.Series(dtype=float), 1_000.0, 30))


# ── ADX ────────────────────────────────────────────────────────────────

def test_wilder_atr_uses_arithmetic_seed_then_wilder_recurrence():
    # Flat closes make the candle ranges the exact true ranges: 1, 2.5, 4, 5, 2.
    ranges = pd.Series([1.0, 2.5, 4.0, 5.0, 2.0], index=_daily_index(5))
    close = pd.Series(100.0, index=ranges.index)
    out = wilder_atr(close + ranges / 2, close - ranges / 2, close, 3)

    assert out.iloc[:2].isna().all()
    assert out.iloc[2:].tolist() == pytest.approx([2.5, 10 / 3, 26 / 9])


def test_wilder_atr_waits_for_fresh_complete_history_after_a_missing_candle():
    ranges = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0], index=_daily_index(6))
    close = pd.Series(100.0, index=ranges.index)
    high = close + ranges / 2
    high.iloc[2] = float("nan")
    out = wilder_atr(high, close - ranges / 2, close, 3)

    assert out.iloc[:5].isna().all()
    assert out.iloc[5] == pytest.approx(5.0)


def test_adx_reaches_100_for_a_one_way_trend():
    idx = _daily_index(60)
    close = pd.Series([100.0 + i for i in range(60)], index=idx)
    out = adx(close + 0.5, close - 0.5, close, 14)
    assert out.iloc[-1] == pytest.approx(100.0)


def test_adx_waits_for_directional_movement_and_adx_warmup():
    idx = _daily_index(27)
    close = pd.Series([100.0 + i for i in range(27)], index=idx)
    assert adx(close + 0.5, close - 0.5, close, 14).isna().all()


def test_adx_stays_low_for_balanced_chop():
    idx = _daily_index(80)
    close = pd.Series([101.0 if i % 2 else 99.0 for i in range(80)], index=idx)
    out = adx(close + 0.5, close - 0.5, close, 14)
    assert out.iloc[-1] < 10.0


# ── Chart quality ─────────────────────────────────────────────────────────────

def _make_clean_daily(n: int = 30) -> pd.DataFrame:
    """Clean trending daily bars: no gaps, decisive candles, not over-extended."""
    idx = _daily_index(n)
    c = pd.Series([100.0 + i * 0.1 for i in range(n)], index=idx)
    return pd.DataFrame(
        {"open": c, "high": c + 0.3, "low": c - 0.1, "close": c},
        index=idx,
    )


def test_chart_quality_range_0_100():
    df = _make_clean_daily(30)
    ema8 = ema(df["close"], 8)
    from scanner.indicators.atr import wilder_atr
    atr = wilder_atr(df["high"], df["low"], df["close"], 5)
    score = chart_quality(df, ema8, atr)
    assert 0.0 <= score <= 100.0


def test_chart_quality_insufficient_history():
    """Fewer than 20 bars returns 50.0 (neutral)."""
    df = _make_clean_daily(10)
    ema8 = ema(df["close"], 8)
    from scanner.indicators.atr import wilder_atr
    atr = wilder_atr(df["high"], df["low"], df["close"], 5)
    assert chart_quality(df, ema8, atr) == 50.0


def test_chart_quality_choppy_scores_lower():
    """Doji-heavy chart (tiny bodies, big wicks) scores lower than decisive candles."""
    from scanner.indicators.atr import wilder_atr

    n = 30
    idx = _daily_index(n)
    closes = [100.0 + i * 0.1 for i in range(n)]

    # Choppy: open == close (doji), wide range -> body_ratio ~ 0
    df_choppy = pd.DataFrame(
        {"open": closes, "high": [c + 1.0 for c in closes],
         "low": [c - 1.0 for c in closes], "close": closes},
        index=idx,
    )
    # Decisive: open far from close, range similar -> body_ratio ~ 0.75
    opens_d = [c - 0.75 for c in closes]
    df_decisive = pd.DataFrame(
        {"open": opens_d, "high": [c + 0.25 for c in closes],
         "low": [o - 0.25 for o in opens_d], "close": closes},
        index=idx,
    )

    ema8_ch = ema(df_choppy["close"],  8)
    atr_ch  = wilder_atr(df_choppy["high"],  df_choppy["low"],  df_choppy["close"],  5)
    ema8_d  = ema(df_decisive["close"], 8)
    atr_d   = wilder_atr(df_decisive["high"], df_decisive["low"], df_decisive["close"], 5)

    score_choppy   = chart_quality(df_choppy,   ema8_ch, atr_ch)
    score_decisive = chart_quality(df_decisive, ema8_d,  atr_d)
    assert score_choppy < score_decisive, (
        f"Choppy chart ({score_choppy}) should score lower than decisive chart ({score_decisive})"
    )


def test_chart_quality_extended_scores_lower():
    """A stock far from its EMA-8 (over-extended) scores lower than one near it."""
    from scanner.indicators.atr import wilder_atr

    n = 30
    idx = _daily_index(n)

    def _decisive(closes):
        opens = [c - 0.5 for c in closes]
        return pd.DataFrame(
            {"open": opens, "high": [c + 0.2 for c in closes],
             "low": [o - 0.2 for o in opens], "close": closes},
            index=idx,
        )

    closes_near = [100.0 + i * 0.1 for i in range(n)]
    # Spike last bar 3.5 ATR away from EMA8 (ATR ~ 1.0 on this chart -> +3.5)
    closes_ext  = closes_near[:-1] + [closes_near[-2] + 3.5]

    df_near = _decisive(closes_near)
    df_ext  = _decisive(closes_ext)

    ema8_n = ema(df_near["close"], 8)
    atr_n  = wilder_atr(df_near["high"], df_near["low"], df_near["close"], 5)
    ema8_e = ema(df_ext["close"],  8)
    atr_e  = wilder_atr(df_ext["high"],  df_ext["low"],  df_ext["close"],  5)

    score_near = chart_quality(df_near, ema8_n, atr_n)
    score_ext  = chart_quality(df_ext,  ema8_e, atr_e)
    assert score_ext < score_near, (
        f"Extended chart ({score_ext}) should score lower than near chart ({score_near})"
    )
