import tempfile
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from scanner.cache import parquet


def _make_df(n: int = 5) -> pd.DataFrame:
    idx = pd.date_range("2024-01-01", periods=n, freq="B", tz="UTC")
    return pd.DataFrame(
        {
            "open": np.random.rand(n) * 10 + 100,
            "high": np.random.rand(n) * 10 + 110,
            "low": np.random.rand(n) * 10 + 90,
            "close": np.random.rand(n) * 10 + 100,
            "volume": np.random.randint(1_000_000, 10_000_000, n).astype(float),
            "vwap": np.random.rand(n) * 10 + 100,
            "trade_count": np.random.randint(10_000, 100_000, n).astype(float),
        },
        index=idx,
    )


def test_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        df = _make_df()
        parquet.save("AAPL", df, Path(tmp))
        loaded = parquet.load("AAPL", Path(tmp))
        pd.testing.assert_frame_equal(df, loaded, check_freq=False)


def test_load_missing_returns_none():
    with tempfile.TemporaryDirectory() as tmp:
        assert parquet.load("NONEXISTENT", Path(tmp)) is None


def test_is_fresh_missing_returns_false():
    with tempfile.TemporaryDirectory() as tmp:
        assert not parquet.is_fresh("AAPL", Path(tmp), date(2024, 1, 5))


def test_is_fresh_when_cache_covers_date():
    with tempfile.TemporaryDirectory() as tmp:
        df = _make_df(5)  # last bar: 2024-01-05 (Friday)
        parquet.save("AAPL", df, Path(tmp))
        last_date = df.index.max().date()
        assert parquet.is_fresh("AAPL", Path(tmp), last_date)


def test_is_fresh_stale_when_asked_for_future_date():
    with tempfile.TemporaryDirectory() as tmp:
        df = _make_df(5)
        parquet.save("AAPL", df, Path(tmp))
        future = date(2025, 1, 1)
        assert not parquet.is_fresh("AAPL", Path(tmp), future)


def test_save_creates_parent_dirs():
    with tempfile.TemporaryDirectory() as tmp:
        nested = Path(tmp) / "a" / "b"
        parquet.save("SPY", _make_df(), nested)
        assert (nested / "SPY.parquet").exists()


def test_interrupted_save_keeps_previous_cache(tmp_path, monkeypatch):
    original = _make_df()
    parquet.save("SPY", original, tmp_path)

    def interrupted(_df, _path):
        raise OSError("interrupted")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        parquet.save("SPY", _make_df(8), tmp_path)
    pd.testing.assert_frame_equal(parquet.load("SPY", tmp_path), original, check_freq=False)
    assert len(list(tmp_path.iterdir())) == 1
