"""Resolve one explicitly selected watchlist into the next scanner universe."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from scanner.json_store import UniverseSelectionStore, WatchlistStore, normalize_symbols


class UniverseSelectionError(ValueError):
    pass


@dataclass(frozen=True)
class ActiveUniverse:
    watchlist_id: str
    name: str
    symbols: list[str]


def load_active_universe(
    watchlists_path: Path = Path("data/watchlists.json"),
    selection_path: Path = Path("data/universe_selection.json"),
) -> ActiveUniverse | None:
    selected = UniverseSelectionStore(selection_path).load()
    if selected is None:
        return None
    watchlist = next((w for w in WatchlistStore(watchlists_path).load_all()
                      if w.get("id") == selected), None)
    if watchlist is None:
        raise UniverseSelectionError(
            f"Selected universe watchlist {selected!r} does not exist. "
            f"Repair {selection_path} or clear the selection in the dashboard."
        )
    symbols = normalize_symbols(watchlist.get("symbols"))
    if not symbols:
        raise UniverseSelectionError(
            f"Selected universe watchlist {watchlist.get('name') or selected!r} is empty."
        )
    return ActiveUniverse(selected, str(watchlist.get("name") or selected), symbols)


def stream_symbols(symbols: list[str], sector_etfs: list[str] | set[str] | tuple[str, ...]) -> list[str]:
    out: list[str] = []
    for symbol in ["SPY", *symbols, *sector_etfs]:
        symbol = str(symbol or "").strip().upper()
        if symbol and symbol not in out:
            out.append(symbol)
    return out


def validate_stream_budget(active: ActiveUniverse, sector_etfs, cap: int = 300) -> dict:
    total_symbols = stream_symbols(active.symbols, sector_etfs)
    support = [s for s in total_symbols if s not in active.symbols]
    if len(total_symbols) > cap:
        raise UniverseSelectionError(
            f"Selected universe {active.name!r} needs {len(total_symbols)} Schwab chart streams "
            f"({len(active.symbols)} watchlist symbols + {len(support)} support symbols), "
            f"but Schwab allows {cap}. Remove at least {len(total_symbols) - cap} symbol(s) "
            "from that watchlist; the scanner will not truncate it silently."
        )
    return {
        "watchlist_count": len(active.symbols),
        "support_count": len(support),
        "total_count": len(total_symbols),
        "cap": cap,
        "support_symbols": support,
    }


__all__ = ["ActiveUniverse", "UniverseSelectionError", "load_active_universe",
           "stream_symbols", "validate_stream_budget"]
