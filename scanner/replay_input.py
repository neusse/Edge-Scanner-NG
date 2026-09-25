"""Frozen historical inputs for a deterministic, offline scanner replay."""
from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import pandas as pd


_ET = ZoneInfo("America/New_York")
_CONFIG_PATHS = (
    Path("data/sector_map.csv"),
    Path("data/setups/custom"),
    Path("data/setups/profiles.json"),
    Path("data/setups/names.json"),
    Path("data/universe/profiles"),
    Path("data/setups/paramsets"),
    Path("data/settings/current.json"),
)


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _prior(frame: pd.DataFrame, target: date, *, intraday: bool) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    days = index.tz_convert(_ET).date if intraday else index.date
    return frame.loc[days < target].sort_index()


def _session(frame: pd.DataFrame, target: date) -> pd.DataFrame:
    if frame is None or frame.empty:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize("UTC")
    return frame.loc[index.tz_convert(_ET).date == target].sort_index()


@dataclass(frozen=True)
class ReplayInput:
    root: Path
    date: date
    symbols: tuple[str, ...]
    sector_map: dict[str, str]
    data_id: str
    config_id: str

    @classmethod
    def capture(cls, feed, target: date, symbols: list[str], sector_map: dict[str, str],
                *, base: Path = Path("data/replay/inputs")) -> "ReplayInput":
        """Fetch one historical session and save every input before evaluation.

        This uses REST history only. It never subscribes to a market stream.
        A failed/incomplete fetch leaves no replayable input bundle.
        """
        universe = tuple(sorted({s.strip().upper() for s in symbols if s.strip()}))
        if not universe:
            raise ValueError("replay needs at least one symbol")
        if any(not re.fullmatch(r"[A-Z0-9._-]{1,16}", symbol) for symbol in universe):
            raise ValueError("replay symbols must be ticker-like names")
        support = sorted({"SPY", *[sector_map[s] for s in universe if sector_map.get(s)]})
        if any(not re.fullmatch(r"[A-Z0-9._-]{1,16}", symbol) for symbol in support):
            raise ValueError("sector symbols must be ticker-like names")
        all_symbols = sorted(set(universe) | set(support))
        root = base / f"{target.isoformat()}-{uuid4().hex[:12]}"
        root.mkdir(parents=True, exist_ok=False)
        try:
            files: dict[str, str] = {}
            for symbol in all_symbols:
                daily = _prior(feed.get_bars_range(symbol, "Day", target - timedelta(days=420),
                                                    target - timedelta(days=1)), target, intraday=False)
                if daily.empty:
                    raise ValueError(f"{symbol}: no prior daily history")
                five = _prior(feed.get_bars_range(symbol, "5Min", target - timedelta(days=35),
                                                   target - timedelta(days=1)), target, intraday=True)
                one = _session(feed.get_bars_range(symbol, "1Min", target, target), target)
                if one.empty:
                    raise ValueError(f"{symbol}: no one-minute bars for {target}")
                for label, frame in (("daily", daily), ("5min", five), ("1min", one)):
                    rel = f"bars/{symbol}-{label}.parquet"
                    path = root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    frame.to_parquet(path)
                    files[rel] = _digest(path)
            for source in _CONFIG_PATHS:
                if not source.exists():
                    continue
                if source.is_file():
                    dest = root / "config" / source.relative_to("data")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, dest)
                    files[dest.relative_to(root).as_posix()] = _digest(dest)
                else:
                    for path in sorted(source.glob("*.json")):
                        dest = root / "config" / path.relative_to("data")
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(path, dest)
                        files[dest.relative_to(root).as_posix()] = _digest(dest)
            # The selected symbols, not the live file's full contents, are the
            # replay universe. Preserve metadata where it exists for V2.
            universe_source = Path("data/universe.csv")
            rows: dict[str, dict] = {}
            fields = ["symbol"]
            if universe_source.exists():
                with universe_source.open(newline="", encoding="utf-8-sig") as handle:
                    reader = csv.DictReader(handle)
                    fields = list(reader.fieldnames or ["symbol"])
                    if "symbol" not in fields:
                        fields.insert(0, "symbol")
                    rows = {row.get("symbol", "").strip().upper(): row for row in reader}
            dest = root / "config/universe.csv"
            dest.parent.mkdir(parents=True, exist_ok=True)
            with dest.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
                writer.writeheader()
                for symbol in universe:
                    writer.writerow({**rows.get(symbol, {}), "symbol": symbol})
            files[dest.relative_to(root).as_posix()] = _digest(dest)
            data_id = hashlib.sha256(json.dumps({k: v for k, v in files.items() if k.startswith("bars/")},
                                                sort_keys=True).encode()).hexdigest()[:16]
            config_id = hashlib.sha256(json.dumps({k: v for k, v in files.items() if k.startswith("config/")},
                                                  sort_keys=True).encode()).hexdigest()[:16]
            manifest = {"schema": 1, "date": target.isoformat(), "symbols": universe,
                        "sector_map": {s: sector_map[s] for s in universe if sector_map.get(s)},
                        "data_id": data_id, "config_id": config_id, "files": files}
            (root / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
            return cls(root, target, universe, manifest["sector_map"], data_id, config_id)
        except BaseException:
            shutil.rmtree(root)
            raise

    @classmethod
    def load(cls, root: Path) -> "ReplayInput":
        root = Path(root)
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        if manifest.get("schema") != 1:
            raise ValueError("unsupported replay input schema")
        for rel, expected in manifest["files"].items():
            if _digest(root / rel) != expected:
                raise ValueError(f"replay input changed: {rel}")
        return cls(root, date.fromisoformat(manifest["date"]), tuple(manifest["symbols"]),
                   manifest["sector_map"], manifest["data_id"], manifest["config_id"])

    def frame(self, symbol: str, timeframe: str) -> pd.DataFrame:
        return pd.read_parquet(self.root / "bars" / f"{symbol}-{timeframe}.parquet")

    def ordered_bars(self, end: str = "20:00") -> list[dict]:
        support = set(self.sector_map.values())
        bars: list[dict] = []
        for symbol in sorted(set(self.symbols) | support | {"SPY"}):
            for stamp, row in self.frame(symbol, "1min").iterrows():
                ts = pd.Timestamp(stamp)
                if ts.tzinfo is None:
                    ts = ts.tz_localize("UTC")
                clock = ts.tz_convert(_ET).strftime("%H:%M")
                # Always replay from the first available bar. Skipping earlier
                # bars would make VWAP, opening range, EMA and cooldown state
                # wrong when the user chooses a later alert-display start.
                if clock > end:
                    continue
                bars.append({"symbol": symbol, "timestamp": ts, "open": float(row["open"]),
                             "high": float(row["high"]), "low": float(row["low"]),
                             "close": float(row["close"]), "volume": float(row["volume"]),
                             "source": "replay_history_1m"})
        bars.sort(key=lambda b: (b["timestamp"], 0 if b["symbol"] == "SPY" else
                                 1 if b["symbol"] in support else 2, b["symbol"]))
        return bars
