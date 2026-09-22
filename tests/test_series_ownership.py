"""LiveScanner owns the SymbolSeries rings; CustomEvaluator borrows them.

The move exists so the universe conditions can read the candle rings whether or
not custom setups are attached. It is only safe if the rings are advanced
exactly once per bar and seeded exactly once at warmup. Both mistakes are
silent: a double advance double-counts every bar's volume (breaking every
relative-volume condition), and a double seed pushes each historical close
through the EMAs twice.
"""
from __future__ import annotations

import pandas as pd
import pytest

from scanner.alert_sink import AlertSink
from scanner.custom_setups import CustomEvaluator, CustomSetupStore
from scanner.live_scanner import LiveScanner
from tests.helpers import FakeSetupEvaluator


def _daily(n: int = 60, base: float = 100.0) -> pd.DataFrame:
    idx = pd.date_range("2020-01-01", periods=n, freq="B", tz="UTC")
    c = pd.Series([base + i * 0.05 for i in range(n)], index=idx)
    return pd.DataFrame({"open": c, "high": c + 0.5, "low": c - 0.5,
                         "close": c, "volume": 1_000_000.0}, index=idx)


def _bar(symbol: str, price: float, et_str: str, volume: float = 5000.0) -> dict:
    ts = pd.Timestamp(et_str, tz="America/New_York").tz_convert("UTC")
    return {"symbol": symbol, "timestamp": ts, "open": price, "high": price + 0.05,
            "low": price - 0.05, "close": price, "volume": volume}


def _scanner(with_custom: bool, tmp_path) -> LiveScanner:
    daily = _daily()
    sc = LiveScanner(["AAA"], None, AlertSink())
    sc.warmup(daily, {"AAA": daily})
    if with_custom:
        sc.attach_system(FakeSetupEvaluator(), AlertSink())
        sc.attach_custom(CustomEvaluator(CustomSetupStore(tmp_path / "setups")))
    return sc


def test_scanner_builds_a_series_for_every_warmed_symbol(tmp_path):
    sc = _scanner(False, tmp_path)
    assert set(sc._series) == {"AAA"}
    assert sc.series("AAA").daily["days"] > 0, "seed_daily should have run"


def test_one_bar_advances_the_rings_exactly_once_without_custom_setups(tmp_path):
    sc = _scanner(False, tmp_path)
    sc._on_bar(_bar("AAA", 100.0, "2024-01-02 10:00", volume=5000.0))
    s = sc.series("AAA")
    assert len(s.m1) == 1
    assert s.m1[-1]["volume"] == 5000.0


def test_one_bar_advances_the_rings_exactly_once_with_custom_setups(tmp_path):
    """The regression that matters: LiveScanner and CustomEvaluator must not
    both push the same bar, or every volume reads double."""
    sc = _scanner(True, tmp_path)
    sc._on_bar(_bar("AAA", 100.0, "2024-01-02 10:00", volume=5000.0))
    s = sc.series("AAA")
    assert len(s.m1) == 1, "bar pushed twice"
    assert s.m1[-1]["volume"] == 5000.0, "volume double-counted"


def test_the_custom_evaluator_shares_the_scanners_rings(tmp_path):
    sc = _scanner(True, tmp_path)
    ce = sc._custom_evaluator
    assert ce.owns_series is False
    assert ce._series is sc._series
    sc._on_bar(_bar("AAA", 100.0, "2024-01-02 10:00"))
    assert ce.series("AAA") is sc.series("AAA")


def test_custom_evaluator_can_stage_on_the_scanners_general_sink(tmp_path):
    daily = _daily()
    sc = LiveScanner(["AAA"], None, AlertSink())
    sc.warmup(daily, {"AAA": daily})
    ce = CustomEvaluator(CustomSetupStore(tmp_path / "setups"))

    sc.attach_custom(ce)

    assert sc._custom_evaluator is ce
    assert sc._custom_sink is sc.sink


def test_a_standalone_custom_evaluator_still_owns_and_advances_its_own(tmp_path):
    """The check() path and the existing tests construct one unattached."""
    ce = CustomEvaluator(CustomSetupStore(tmp_path / "setups"))
    assert ce.owns_series is True
    daily = _daily()
    ce.warmup({"AAA": daily}, None)
    assert ce.series("AAA").daily["days"] > 0


def test_warmup_seeds_the_emas_once_not_twice(tmp_path):
    """CustomEvaluator.warmup must not re-seed a shared, already-seeded ring."""
    daily = _daily()
    sc = LiveScanner(["AAA"], None, AlertSink())
    sc.warmup(daily, {"AAA": daily})
    sc.attach_system(FakeSetupEvaluator(), AlertSink())
    ce = CustomEvaluator(CustomSetupStore(tmp_path / "setups"))
    sc.attach_custom(ce)

    before = dict(sc.series("AAA").daily)
    ce.warmup({"AAA": daily}, None)          # what run_live.py does after attaching
    assert sc.series("AAA").daily == before, "shared rings were seeded a second time"


def test_series_is_created_lazily_for_a_symbol_warmup_skipped(tmp_path):
    sc = _scanner(False, tmp_path)
    assert "ZZZ" not in sc._series
    s = sc.series("ZZZ")
    assert s.symbol == "ZZZ" and sc._series["ZZZ"] is s


def test_the_session_tag_flows_from_the_scanner_to_the_custom_evaluator(tmp_path):
    """CustomEvaluator must use the passed tag, not recompute by advancing."""
    sc = _scanner(True, tmp_path)
    sc._on_bar(_bar("AAA", 100.0, "2024-01-02 08:00"))   # premarket
    assert sc.series("AAA").m1[-1]["session"] == "pre"
    sc._on_bar(_bar("AAA", 100.0, "2024-01-02 10:00"))   # RTH
    assert sc.series("AAA").m1[-1]["session"] == "rth"
    assert len(sc.series("AAA").m1) == 2
