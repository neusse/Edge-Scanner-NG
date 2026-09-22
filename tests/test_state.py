"""Tests for SymbolState — initialization from history and live bar updates."""
import pandas as pd
import pytest

from scanner.indicators.ema_sma import sma
from scanner.indicators.rrs import D1_LENGTH, rrs as compute_rrs
from scanner.indicators.atr import wilder_atr
from scanner.state import SymbolState, _five_min_slot, _aggregate_into_slot
from collections import deque


# ── Helpers ───────────────────────────────────────────────────────────────────

def _daily_bars(closes: list[float], spread: float = 0.5) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=len(closes), freq="B", tz="UTC")
    c = pd.Series(closes, index=idx)
    return pd.DataFrame(
        {"open": c, "high": c + spread, "low": c - spread, "close": c, "volume": 1_000_000.0},
        index=idx,
    )


def _bar(ts_et: str, price: float, volume: float = 100_000.0) -> dict:
    """Build a 1-min bar dict with an ET timestamp string like '2024-01-02 10:00'."""
    ts = pd.Timestamp(ts_et, tz="America/New_York").tz_convert("UTC")
    return {
        "symbol": "TEST",
        "timestamp": ts,
        "open": price, "high": price + 0.05,
        "low": price - 0.05, "close": price,
        "volume": volume,
    }


def _make_state(n: int = 250) -> SymbolState:
    """Build a SymbolState with n daily bars of flat data."""
    closes = [100.0 + i * 0.05 for i in range(n)]
    stock = _daily_bars(closes)
    spy   = _daily_bars([200.0 + i * 0.1 for i in range(n)])
    return SymbolState.from_history("AAPL", stock, spy)


# ── from_history: prior-day levels ────────────────────────────────────────────

def test_prior_levels_match_last_bar():
    stock = _daily_bars([100.0, 101.0, 102.0, 103.0, 104.0])
    spy   = _daily_bars([200.0, 201.0, 202.0, 203.0, 204.0])
    state = SymbolState.from_history("X", stock, spy)
    assert state.prior_close == pytest.approx(104.0)
    assert state.prior_high  == pytest.approx(104.5)
    assert state.prior_low   == pytest.approx(103.5)


# ── from_history: SMAs ────────────────────────────────────────────────────────

def test_sma_50_matches_pandas():
    state = _make_state(250)
    closes = [100.0 + i * 0.05 for i in range(250)]
    stock = _daily_bars(closes)
    expected = float(sma(stock["close"], 50).iloc[-1])
    assert state.sma_50 == pytest.approx(expected, rel=1e-6)


def test_sma_200_matches_pandas():
    state = _make_state(250)
    closes = [100.0 + i * 0.05 for i in range(250)]
    stock = _daily_bars(closes)
    expected = float(sma(stock["close"], 200).iloc[-1])
    assert state.sma_200 == pytest.approx(expected, rel=1e-6)


def test_sma_200_none_when_insufficient_history():
    state = _make_state(50)  # only 50 bars
    assert state.sma_200 is None


# ── from_history: RRS D1 ──────────────────────────────────────────────────────

def test_rrs_d1_matches_direct_calculation():
    closes = [100.0 + i * 0.05 for i in range(50)]
    stock = _daily_bars(closes)
    spy   = _daily_bars([200.0 + i * 0.1 for i in range(50)])
    state = SymbolState.from_history("X", stock, spy)
    expected = float(compute_rrs(stock, spy, D1_LENGTH).iloc[-1])
    assert state.rrs_d1 == pytest.approx(expected, rel=1e-6)


def test_rrs_sector_none_when_not_provided():
    state = _make_state(30)
    assert state.rrs_sector_d1 is None


def test_rrs_sector_computed_when_provided():
    n = 50
    stock  = _daily_bars([100.0 + i * 0.05 for i in range(n)])
    spy    = _daily_bars([200.0 + i * 0.1  for i in range(n)])
    sector = _daily_bars([50.0  + i * 0.02 for i in range(n)])
    state = SymbolState.from_history("X", stock, spy, sector_daily=sector)
    assert state.rrs_sector_d1 is not None


def test_daily_atr_14_is_computed_for_extension_filters():
    n = 50
    stock = _daily_bars([100.0 + i * 0.25 for i in range(n)], spread=1.0)
    spy = _daily_bars([200.0 + i * 0.1 for i in range(n)])
    state = SymbolState.from_history("X", stock, spy)
    expected = float(wilder_atr(stock["high"], stock["low"], stock["close"], 14).iloc[-1])
    assert state.atr_14_d1 == pytest.approx(expected)


def test_intraday_sector_rrs_uses_completed_five_minute_bars():
    state = _make_state(50)
    start = pd.Timestamp("2024-01-02 09:30", tz="America/New_York")
    for i in range(66):
        stamp = (start + pd.Timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M")
        stock = _bar(stamp, 100.0 + i * 0.10)
        sector = _bar(stamp, 50.0 + i * 0.01)
        sector["symbol"] = "XLK"
        state.on_bar(stock, sector_bar=sector)
    assert state.rrs_sector_m5 is not None
    assert state.rrs_sector_m5 > 0


def test_opening_candle_direction_and_same_slot_rvol():
    state = _make_state(50)
    state.volume_profile = pd.Series({0: 500.0, 5: 250.0})
    for i in range(6):
        bar = _bar(f"2024-01-02 09:{30 + i:02d}", 100.0 + i, volume=200.0)
        state.on_bar(bar)

    assert state.opening_candle_direction == 1.0
    assert state.opening_rvol_m5 == pytest.approx(2.0)


def test_opening_values_wait_for_completed_first_five_minute_candle():
    state = _make_state(50)
    state.volume_profile = pd.Series({0: 500.0})
    for i in range(5):
        state.on_bar(_bar(f"2024-01-02 09:{30 + i:02d}", 100.0, volume=100.0))
    assert state.opening_candle_direction is None
    assert state.opening_rvol_m5 is None


def test_opening_rank_metadata_resets_with_the_session():
    state = _make_state(50)
    state.set_opening_rvol_rank(3.0, 80, 100)
    assert state.opening_rvol_rank == 3.0
    assert state.opening_rvol_coverage == pytest.approx(0.8)
    state._reset_intraday()
    assert state.opening_rvol_rank is None
    assert state.opening_rvol_population == 0


# ── from_history: chart quality ───────────────────────────────────────────────

def test_chart_quality_in_range():
    state = _make_state(30)
    assert 0.0 <= state.chart_quality <= 100.0


# ── on_bar: VWAP ──────────────────────────────────────────────────────────────

def test_vwap_single_bar_equals_typical_price():
    state = _make_state(30)
    b = _bar("2024-01-02 10:00", price=100.0, volume=1_000.0)
    state.on_bar(b)
    # tp = (100.05 + 99.95 + 100.0) / 3 = 100.0
    expected_tp = (b["high"] + b["low"] + b["close"]) / 3.0
    assert state.vwap == pytest.approx(expected_tp)


def test_vwap_uniform_price():
    state = _make_state(30)
    for i in range(5):
        state.on_bar(_bar(f"2024-01-02 10:0{i}", price=150.0, volume=1_000.0))
    assert state.vwap == pytest.approx(150.0, rel=1e-6)


# ── on_bar: high / low of day ─────────────────────────────────────────────────

def test_high_low_of_day():
    state = _make_state(30)
    prices = [100.0, 103.0, 98.0, 101.0]
    for i, p in enumerate(prices):
        state.on_bar(_bar(f"2024-01-02 10:0{i}", price=p))
    assert state.high_of_day == pytest.approx(max(p + 0.05 for p in prices))
    assert state.low_of_day  == pytest.approx(min(p - 0.05 for p in prices))


def test_high_low_none_before_first_bar():
    state = _make_state(30)
    assert state.high_of_day is None
    assert state.low_of_day  is None


# ── on_bar: prev HOD/LOD ──────────────────────────────────────────────────────

def test_prev_hod_lod_none_before_first_bar():
    state = _make_state(30)
    assert state.prev_hod is None
    assert state.prev_lod is None


def test_prev_hod_is_none_after_first_bar():
    state = _make_state(30)
    state.on_bar(_bar("2024-01-02 10:00", price=100.0))
    # After bar 1: prev_hod was None (no prior HOD before this bar)
    assert state.prev_hod is None
    assert state.prev_lod is None


def test_prev_hod_reflects_hod_before_current_bar():
    state = _make_state(30)
    # Bar 1: high = 100.05, low = 99.95
    state.on_bar(_bar("2024-01-02 10:00", price=100.0))
    hod_after_bar1 = state.high_of_day   # = 100.05
    lod_after_bar1 = state.low_of_day    # = 99.95

    # Bar 2: higher price — prev_hod should equal hod_after_bar1
    state.on_bar(_bar("2024-01-02 10:01", price=102.0))
    assert state.prev_hod == pytest.approx(hod_after_bar1)
    assert state.prev_lod == pytest.approx(lod_after_bar1)


def test_prev_hod_not_updated_when_hod_not_broken():
    state = _make_state(30)
    state.on_bar(_bar("2024-01-02 10:00", price=103.0))  # HOD = 103.05
    state.on_bar(_bar("2024-01-02 10:01", price=101.0))  # below HOD
    state.on_bar(_bar("2024-01-02 10:02", price=102.0))  # prev_hod still = 103.05

    assert state.prev_hod == pytest.approx(103.05)


# ── on_bar: cumulative volume ─────────────────────────────────────────────────

def test_cumulative_volume():
    state = _make_state(30)
    for i in range(3):
        state.on_bar(_bar(f"2024-01-02 10:0{i}", price=100.0, volume=500_000.0))
    assert state._cum_vol == pytest.approx(1_500_000.0)


# ── on_bar: EMA 3/8 ──────────────────────────────────────────────────────────

def test_ema_seeds_on_first_completed_5m_bar():
    state = _make_state(30)
    # Fill the 09:30-09:34 slot — no bar completes yet
    for t in ["09:30", "09:31", "09:32", "09:33", "09:34"]:
        state.on_bar(_bar(f"2024-01-02 {t}", price=100.0))
    assert state.ema_3 is None
    # 09:35 starts a new slot → completes the 09:30 bar (close=100.0)
    state.on_bar(_bar("2024-01-02 09:35", price=101.0))
    assert state.ema_3 == pytest.approx(100.0)
    assert state.ema_9 == pytest.approx(100.0)


def test_ema_prev_values_lag_by_one_5m_bar():
    state = _make_state(30)
    # Complete first 5-min bar (close=100) by triggering a new slot at 09:35
    for t in ["09:30", "09:31", "09:32", "09:33", "09:34"]:
        state.on_bar(_bar(f"2024-01-02 {t}", price=100.0))
    state.on_bar(_bar("2024-01-02 09:35", price=105.0))
    first_ema3, first_ema9 = state.ema_3, state.ema_9  # seeded from close=100

    # Complete second 5-min bar (close=105) by triggering a new slot at 09:40
    for t in ["09:36", "09:37", "09:38", "09:39"]:
        state.on_bar(_bar(f"2024-01-02 {t}", price=105.0))
    state.on_bar(_bar("2024-01-02 09:40", price=110.0))
    assert state.prev_ema_3 == pytest.approx(first_ema3)
    assert state.prev_ema_9 == pytest.approx(first_ema9)


# ── 5-min aggregation helpers ─────────────────────────────────────────────────

def test_five_min_slot_floor():
    """Timestamps within the same 5-min block map to the same slot."""
    ts_930 = pd.Timestamp("2024-01-02 09:30", tz="America/New_York").tz_convert("UTC")
    ts_931 = pd.Timestamp("2024-01-02 09:31", tz="America/New_York").tz_convert("UTC")
    ts_934 = pd.Timestamp("2024-01-02 09:34", tz="America/New_York").tz_convert("UTC")
    ts_935 = pd.Timestamp("2024-01-02 09:35", tz="America/New_York").tz_convert("UTC")
    assert _five_min_slot(ts_930) == _five_min_slot(ts_931)
    assert _five_min_slot(ts_930) == _five_min_slot(ts_934)
    assert _five_min_slot(ts_930) != _five_min_slot(ts_935)


def test_aggregate_into_slot_completes_on_new_slot():
    """A bar in a new slot finalises the previous partial and starts a new one."""
    completed = deque()
    b1 = _bar("2024-01-02 09:30", price=100.0, volume=1_000.0)
    b2 = _bar("2024-01-02 09:31", price=101.0, volume=2_000.0)
    b3 = _bar("2024-01-02 09:35", price=102.0, volume=3_000.0)  # new slot

    slot1 = _five_min_slot(b1["timestamp"])
    partial = _aggregate_into_slot(None, completed, b1, slot1)
    partial = _aggregate_into_slot(partial, completed, b2, slot1)
    assert len(completed) == 0  # same slot — not completed yet

    slot2 = _five_min_slot(b3["timestamp"])
    partial = _aggregate_into_slot(partial, completed, b3, slot2)
    assert len(completed) == 1  # previous slot finalised

    done = completed[0]
    assert done["open"]   == pytest.approx(100.0)   # open from first bar
    assert done["close"]  == pytest.approx(101.0)   # close from last bar
    assert done["volume"] == pytest.approx(3_000.0) # sum


# ── reset_intraday ────────────────────────────────────────────────────────────

def test_reset_intraday_clears_state():
    state = _make_state(30)
    for i in range(5):
        state.on_bar(_bar(f"2024-01-02 10:0{i}", price=100.0 + i))
    state._reset_intraday()
    assert state.vwap is None
    assert state.high_of_day is None
    assert state.ema_3 is None
    assert state._cum_vol == 0.0
