"""Unit tests for LiveScanner — warmup, bar routing, reset, and alert path."""
from typing import Callable, Optional
from unittest.mock import MagicMock

import pandas as pd
import pytest

from scanner.alert_sink import AlertSink
from scanner.data.interface import DataFeed, Timeframe
from scanner.live_scanner import LiveScanner
from scanner.market import MarketRegime


# ── Fake feed for testing (non-blocking) ─────────────────────────────────────

class _FakeFeed(DataFeed):
    """DataFeed that stores the callback for synchronous test control."""

    def __init__(self):
        self._callback: Optional[Callable] = None

    def subscribe_minute_bars(self, symbols, callback):
        self._callback = callback  # non-blocking: just register

    def push(self, bar: dict) -> None:
        if self._callback:
            self._callback(bar)

    def get_historical_daily(self, symbol, start, end):
        raise NotImplementedError

    def get_historical_bars(self, symbol, timeframe, start, end):
        raise NotImplementedError

    def get_snapshot(self, symbols):
        raise NotImplementedError


# ── Synthetic history helpers ─────────────────────────────────────────────────

def _daily_bars(n: int = 60, base: float = 100.0, step: float = 0.05) -> pd.DataFrame:
    closes = [base + i * step for i in range(n)]
    idx = pd.date_range("2020-01-01", periods=n, freq="B", tz="UTC")
    c = pd.Series(closes, index=idx)
    return pd.DataFrame(
        {"open": c, "high": c + 0.5, "low": c - 0.5, "close": c, "volume": 1_000_000.0},
        index=idx,
    )


def _bar(symbol: str, price: float, et_str: str = "2024-01-02 10:00") -> dict:
    ts = pd.Timestamp(et_str, tz="America/New_York").tz_convert("UTC")
    return {
        "symbol": symbol, "timestamp": ts,
        "open": price, "high": price + 0.05, "low": price - 0.05,
        "close": price, "volume": 100_000.0,
    }


def _spy_bar(price: float = 450.0, et_str: str = "2024-01-02 10:00") -> dict:
    return _bar("SPY", price, et_str)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_scanner(symbols=("AAPL",)) -> tuple[LiveScanner, _FakeFeed, AlertSink]:
    feed = _FakeFeed()
    sink = AlertSink()
    scanner = LiveScanner(list(symbols), feed, sink)
    return scanner, feed, sink


def _system_alert(symbol: str = "AAPL", ts: str = "2024-01-02T10:00:00-05:00", setup: str = "A") -> dict:
    """What a system-setups evaluator hands the scanner."""
    return {"symbol": symbol, "direction": "long", "trigger": f"SYS_{setup}", "setup": setup,
            "score": 75, "timestamp": ts, "price": 101.0, "conditions": {}}


def _warmup(scanner: LiveScanner, symbols=("AAPL",)):
    spy_daily = _daily_bars(60, base=450.0, step=0.1)
    sym_daily = {sym: _daily_bars(60, base=100.0 + i, step=0.05)
                 for i, sym in enumerate(symbols)}
    scanner.warmup(spy_daily, sym_daily)


# ── Warmup ────────────────────────────────────────────────────────────────────

def test_warmup_builds_spy_state():
    scanner, _, _ = _make_scanner()
    _warmup(scanner)
    assert scanner._spy_state is not None


def test_warmup_builds_symbol_states():
    scanner, _, _ = _make_scanner(symbols=["AAPL", "MSFT"])
    _warmup(scanner, symbols=["AAPL", "MSFT"])
    assert "AAPL" in scanner._states
    assert "MSFT" in scanner._states


def test_warmup_skips_missing_symbol():
    scanner, _, _ = _make_scanner(symbols=["AAPL", "NVDA"])
    spy = _daily_bars(60, base=450.0)
    # Only provide AAPL, not NVDA
    scanner.warmup(spy, {"AAPL": _daily_bars(60)})
    assert "AAPL" in scanner._states
    assert "NVDA" not in scanner._states


def test_warmup_prior_close_is_last_bar():
    scanner, _, _ = _make_scanner()
    spy = _daily_bars(60, base=450.0)
    aapl = _daily_bars(60, base=100.0, step=0.05)
    scanner.warmup(spy, {"AAPL": aapl})
    expected_close = float(aapl["close"].iloc[-1])
    assert scanner._states["AAPL"].prior_close == pytest.approx(expected_close)


def test_opening_rvol_rank_is_cross_sectional_and_tracks_coverage():
    scanner, _, _ = _make_scanner(symbols=["AAA", "BBB"])
    _warmup(scanner, symbols=["AAA", "BBB"])
    scanner._states["AAA"].volume_profile = pd.Series({0: 500.0})
    scanner._states["BBB"].volume_profile = pd.Series({0: 500.0})

    for symbol, volume in (("AAA", 200.0), ("BBB", 100.0)):
        for minute in range(30, 36):
            assert scanner.seed_session_bar(
                _bar(symbol, 100.0 + minute / 100.0, f"2024-01-02 09:{minute:02d}")
                | {"volume": volume})

    aaa, bbb = scanner._states["AAA"], scanner._states["BBB"]
    assert aaa.opening_rvol_m5 == pytest.approx(2.0)
    assert bbb.opening_rvol_m5 == pytest.approx(1.0)
    assert aaa.opening_rvol_rank == 1.0
    assert bbb.opening_rvol_rank == 2.0
    assert aaa.opening_rvol_population == 2
    assert aaa.opening_rvol_coverage == pytest.approx(1.0)


def test_opening_rvol_ties_share_the_same_rank():
    scanner, _, _ = _make_scanner(symbols=["AAA", "BBB"])
    _warmup(scanner, symbols=["AAA", "BBB"])
    for state in scanner._states.values():
        state.volume_profile = pd.Series({0: 500.0})
        state._stock_5m.append({"slot": 570, "open": 100.0, "close": 101.0,
                                "high": 101.0, "low": 100.0, "volume": 500.0})
    scanner._refresh_opening_rvol_ranks()
    assert scanner._states["AAA"].opening_rvol_rank == 1.0
    assert scanner._states["BBB"].opening_rvol_rank == 1.0


def test_seed_session_bar_keeps_state_and_custom_trigger_series_in_sync():
    """A mid-session restart must not make a lower live bar look like a new HOD."""
    from scanner.trigger_catalog import EvalCtx, evaluate

    scanner, _, _ = _make_scanner()
    _warmup(scanner)
    state = scanner._states["AAPL"]
    state._reset_intraday()

    seeded = _bar("AAPL", 105.0, "2024-01-02 09:30")
    scanner.seed_session_bar(seeded)

    assert state.high_of_day == seeded["high"]
    assert scanner.series("AAPL").day_high == seeded["high"]

    lower = _bar("AAPL", 104.0, "2024-01-02 09:31")
    state.on_bar(lower)
    session = scanner._advance_series(state, lower)
    ctx = EvalCtx(state=state, series=scanner.series("AAPL"), bar=lower,
                  et_min=9 * 60 + 31, session=session, external=set())
    assert evaluate("hod", ctx, "high", {}) is None


# ── SPY bar routing ───────────────────────────────────────────────────────────

def test_spy_bar_updates_latest_spy_bar():
    scanner, _, _ = _make_scanner()
    _warmup(scanner)
    b = _spy_bar(price=450.0)
    scanner._on_bar(b)
    assert scanner._latest_spy_bar is b


def test_spy_bar_above_vwap_sets_bullish_regime():
    scanner, _, _ = _make_scanner()
    _warmup(scanner)
    # Push many bars at 450 to anchor VWAP near 450
    for i in range(5):
        scanner._on_bar(_spy_bar(price=450.0, et_str=f"2024-01-02 10:0{i}"))
    # Now push a bar significantly above VWAP
    scanner._on_bar(_spy_bar(price=456.0, et_str="2024-01-02 10:05"))
    assert scanner._regime == MarketRegime.BULLISH


def test_spy_bar_below_vwap_sets_bearish_regime():
    scanner, _, _ = _make_scanner()
    _warmup(scanner)
    for i in range(5):
        scanner._on_bar(_spy_bar(price=450.0, et_str=f"2024-01-02 10:0{i}"))
    scanner._on_bar(_spy_bar(price=444.0, et_str="2024-01-02 10:05"))
    assert scanner._regime == MarketRegime.BEARISH


# ── Symbol bar routing ────────────────────────────────────────────────────────

def test_unknown_symbol_is_noop():
    scanner, _, _ = _make_scanner(symbols=["AAPL"])
    _warmup(scanner, symbols=["AAPL"])
    # Push bar for symbol not in universe
    scanner._on_bar(_bar("NVDA", 500.0))  # should not raise


def test_symbol_bar_updates_high_of_day():
    scanner, _, _ = _make_scanner()
    _warmup(scanner)
    state = scanner._states["AAPL"]
    assert state.high_of_day is None  # before any intraday bar
    scanner._on_bar(_bar("AAPL", price=105.0))
    assert state.high_of_day == pytest.approx(105.05)  # high = price + 0.05


def test_symbol_bar_updates_cumulative_volume():
    scanner, _, _ = _make_scanner()
    _warmup(scanner)
    state = scanner._states["AAPL"]
    scanner._on_bar(_bar("AAPL", price=100.0))
    scanner._on_bar(_bar("AAPL", price=100.0, et_str="2024-01-02 10:01"))
    assert state._cum_vol == pytest.approx(200_000.0)


def test_symbol_bar_seeds_ema_on_first_completed_5m_bar():
    scanner, _, _ = _make_scanner()
    _warmup(scanner)
    state = scanner._states["AAPL"]
    # Fill the 10:00-10:04 slot — no 5-min bar completes yet
    for minute in ["10:00", "10:01", "10:02", "10:03", "10:04"]:
        scanner._on_bar(_bar("AAPL", price=100.0, et_str=f"2024-01-02 {minute}"))
    assert state.ema_3 is None
    # 10:05 starts a new slot → completes the 10:00 bar (close=100.0)
    scanner._on_bar(_bar("AAPL", price=101.0, et_str="2024-01-02 10:05"))
    assert state.ema_3 == pytest.approx(100.0)
    assert state.ema_9 == pytest.approx(100.0)


# ── reset_session ─────────────────────────────────────────────────────────────

def test_reset_session_clears_intraday_vwap():
    scanner, _, _ = _make_scanner()
    _warmup(scanner)
    scanner._on_bar(_bar("AAPL", price=100.0))
    assert scanner._states["AAPL"].vwap is not None
    scanner.reset_session()
    assert scanner._states["AAPL"].vwap is None


def test_reset_session_clears_spy_intraday():
    scanner, _, _ = _make_scanner()
    _warmup(scanner)
    scanner._on_bar(_spy_bar(price=450.0))
    assert scanner._spy_state.vwap is not None
    scanner.reset_session()
    assert scanner._spy_state.vwap is None


def test_reset_session_clears_regime():
    scanner, _, _ = _make_scanner()
    _warmup(scanner)
    for i in range(5):
        scanner._on_bar(_spy_bar(450.0, f"2024-01-02 10:0{i}"))
    scanner._on_bar(_spy_bar(456.0, "2024-01-02 10:05"))
    assert scanner._regime == MarketRegime.BULLISH
    scanner.reset_session()
    assert scanner._regime == MarketRegime.NEUTRAL


def test_reset_session_clears_sink():
    scanner, _, sink = _make_scanner()
    _warmup(scanner)
    fake_alert = {
        "symbol": "AAPL", "direction": "long", "trigger": "ema_cross",
        "score": 75, "timestamp": "2024-01-02T10:00:00-05:00",
        "price": 101.0, "conditions": {},
    }
    sink.push(fake_alert)
    assert len(sink) == 1
    scanner.reset_session()
    assert len(sink) == 0


# ── system setups (attach_system) ────────────────────────────────────────────

def test_attach_system_routes_symbol_bars_to_the_system_sink():
    scanner, _, _ = _make_scanner()
    _warmup(scanner)
    system_sink = AlertSink()
    system_eval = MagicMock()
    system_eval.on_bar.return_value = [_system_alert()]
    scanner.attach_system(system_eval, system_sink)

    scanner._on_bar(_spy_bar(price=450.0))
    system_eval.on_bar.assert_not_called()
    scanner._on_bar(_bar("AAPL", price=101.0))
    system_eval.on_bar.assert_called_once()
    assert len(system_sink) == 1 and system_sink.top()[0]["trigger"] == "SYS_A"


# ── connect (non-blocking with FakeFeed) ─────────────────────────────────────

def test_connect_registers_callback():
    scanner, feed, _ = _make_scanner()
    _warmup(scanner)
    scanner.connect()  # non-blocking because FakeFeed.subscribe_minute_bars just stores callback
    # Bound methods compare equal (==) when wrapping the same function on the same instance;
    # identity (is) always fails because Python creates a new object on each attribute access.
    assert feed._callback == scanner._on_bar


def test_connect_includes_spy_and_all_symbols():
    scanner, feed, _ = _make_scanner(symbols=["AAPL", "MSFT"])
    _warmup(scanner, symbols=["AAPL", "MSFT"])
    subscribed: list = []
    original = feed.subscribe_minute_bars
    def _capture(symbols, callback):
        subscribed.extend(symbols)
        feed._callback = callback
    feed.subscribe_minute_bars = _capture
    scanner.connect()
    assert "SPY"  in subscribed
    assert "AAPL" in subscribed
    assert "MSFT" in subscribed


def test_connect_adds_sector_etfs_to_the_same_subscription():
    feed = _FakeFeed()
    scanner = LiveScanner(["AAPL", "MSFT"], feed, sector_map={"AAPL": "XLK", "MSFT": "XLK"})
    spy = _daily_bars(60, base=450.0)
    stocks = {"AAPL": _daily_bars(60), "MSFT": _daily_bars(60, base=200.0)}
    scanner.warmup(spy, stocks, sector_daily={"XLK": _daily_bars(60, base=150.0)})
    scanner.connect()
    assert feed._callback == scanner._on_bar
    assert feed._callback is not None
    # One call, one stream, with the shared reference symbol included once.
    subscribed = ["SPY"] + scanner.ranked_symbols() + ["XLK"]
    captured = []
    feed.subscribe_minute_bars = lambda symbols, callback: captured.extend(symbols)
    scanner.connect()
    assert captured == subscribed


def test_sector_bar_is_routed_as_reference_data_not_an_alert_symbol():
    feed = _FakeFeed()
    scanner = LiveScanner(["AAPL"], feed, sector_map={"AAPL": "XLK"})
    spy = _daily_bars(60, base=450.0)
    scanner.warmup(spy, {"AAPL": _daily_bars(60)},
                   sector_daily={"XLK": _daily_bars(60, base=150.0)})
    sector_bar = _bar("XLK", 150.0, "2024-01-02 10:00")
    scanner._on_bar(sector_bar)
    scanner._on_bar(_bar("AAPL", 100.0, "2024-01-02 10:00"))
    assert scanner._latest_sector_bars["XLK"] is sector_bar
    assert scanner._states["AAPL"]._partial_sector_5m is not None


def test_connect_subscribes_most_liquid_first():
    """A provider that caps its best data tier takes symbols in this order."""
    from types import SimpleNamespace
    from scanner.live_scanner import LiveScanner
    got = []
    sc = LiveScanner.__new__(LiveScanner)
    sc.feed = SimpleNamespace(subscribe_minute_bars=lambda symbols, cb: got.extend(symbols))
    sc._states = {
        "AAA": SimpleNamespace(adv20=1_000_000, prior_close=5.0),        # $5M
        "NVDA": SimpleNamespace(adv20=100_000_000, prior_close=200.0),   # $20B
        "ZZZ": SimpleNamespace(adv20=None, prior_close=10.0),            # unknown: last
        "MU": SimpleNamespace(adv20=20_000_000, prior_close=100.0),      # $2B
    }
    sc.connect()
    assert got == ["SPY", "NVDA", "MU", "AAA", "ZZZ"]

