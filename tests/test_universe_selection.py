from pathlib import Path

import pytest

from scanner.json_store import UniverseSelectionStore, WatchlistStore
from scanner.universe_selection import (
    ActiveUniverse, UniverseSelectionError, load_active_universe, stream_symbols,
    validate_stream_budget,
)


def test_load_active_universe_is_explicit_and_out_of_band_editable(tmp_path: Path):
    watchlists = tmp_path / "watchlists.json"
    selection = tmp_path / "selection.json"
    WatchlistStore(watchlists).save({"id": "focus", "name": "Focus", "symbols": [" aapl ", "MSFT", "AAPL"]})
    assert load_active_universe(watchlists, selection) is None
    UniverseSelectionStore(selection).save("focus")
    active = load_active_universe(watchlists, selection)
    assert active == ActiveUniverse("focus", "Focus", ["AAPL", "MSFT"])


def test_missing_or_empty_selected_watchlist_refuses_start(tmp_path: Path):
    watchlists = tmp_path / "watchlists.json"
    selection = tmp_path / "selection.json"
    UniverseSelectionStore(selection).save("missing")
    with pytest.raises(UniverseSelectionError, match="does not exist"):
        load_active_universe(watchlists, selection)
    WatchlistStore(watchlists).save({"id": "missing", "name": "Empty", "symbols": []})
    with pytest.raises(UniverseSelectionError, match="is empty"):
        load_active_universe(watchlists, selection)


def test_stream_budget_counts_support_and_never_truncates():
    symbols = [f"S{i}" for i in range(289)]
    active = ActiveUniverse("large", "Large", symbols)
    support = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLRE", "XLU", "XLC"]
    assert len(stream_symbols(symbols[:288], support)) == 300
    with pytest.raises(UniverseSelectionError, match="will not truncate"):
        validate_stream_budget(active, support, cap=300)
