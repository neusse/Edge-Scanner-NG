#!/usr/bin/env python
"""Refresh the exact Schwab daily/5-minute caches needed by the next start.

REST only: never constructs a schwabdev.Stream. Reuses the provider's existing
incremental coverage planner, per-symbol Parquet files and shared rate limiter.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(override=True)

from scanner.instance_lock import ScannerInstanceLock
from scanner.market_session import session_close
from scanner.universe_selection import load_active_universe

ET = ZoneInfo("America/New_York")
DAILY_DAYS = 380
INTRADAY_DAYS = 20


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _target_session(now: datetime, requested: date | None = None) -> date | None:
    target = requested or now.astimezone(ET).date()
    close = session_close(target)
    if close is None:
        if requested:
            raise ValueError(f"{target} is not an NYSE session")
        return None
    if target > now.astimezone(ET).date() or (
            target == now.astimezone(ET).date()
            and now.astimezone(timezone.utc) < close + timedelta(minutes=30)):
        raise ValueError(f"{target} history is not ready until 30 minutes after its NYSE close")
    return target


def _write_result(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def refresh(*, session: date | None = None, universe: Path | None = None,
            dry_run: bool = False, live_port: int = 7777,
            result_path: Path = Path("data/schwab/history_refresh.json")) -> dict:
    now = datetime.now(timezone.utc)
    target = _target_session(now, session)
    if target is None:
        return {"status": "skipped", "reason": "NYSE closed", "checked_at": now.isoformat()}

    if dry_run:
        # Never construct the Schwab client or alter cache/universe in a dry run.
        from scanner.universe import load_universe
        path = universe or (Path("data/universe_all.csv") if Path("data/universe_all.csv").exists()
                            else Path("data/universe.csv"))
        active = load_active_universe()
        symbols = active.symbols if active else load_universe(path)
        return {"status": "dry_run", "session": target.isoformat(), "universe": str(path),
                "symbols": len(symbols), "active_watchlist": active.name if active else None}

    # New live/replay/refresh processes hold the same OS lock. The listener
    # check also catches a scanner launched before this lock was introduced.
    with ScannerInstanceLock(Path("data/.scanner-instance.lock"), "history-refresh"):
        if _port_listening(live_port):
            raise RuntimeError(f"live scanner is still listening on {live_port}; history refresh skipped")

        if not os.environ.get("SCHWAB_APP_KEY") and os.environ.get("SCHWAB_API_KEY"):
            os.environ["SCHWAB_APP_KEY"] = os.environ["SCHWAB_API_KEY"]
        from scripts.import_schwab_py_token import main as import_shared_token
        import_shared_token()

        # Resolve the SAME effective universe and sector support as the live
        # launcher, moving any weekly rebuild/sector-map work out of morning.
        from scripts.run_live import _ensure_sector_map, _ensure_universe
        path = universe or (Path("data/universe_all.csv") if Path("data/universe_all.csv").exists()
                            else Path("data/universe.csv"))
        symbols = _ensure_universe(False, path, "schwab")
        active = load_active_universe()
        if active is not None:
            symbols = active.symbols
        _, sector_etfs = _ensure_sector_map(symbols)
        daily_symbols = list(dict.fromkeys(["SPY", *sector_etfs, *symbols]))

        from scanner.data.schwab import SchwabFeed
        feed = SchwabFeed()

        def progress(done: int, total: int) -> None:
            if done % 250 == 0 or done == total:
                print(f"  {done}/{total} symbols", flush=True)

        daily = feed.get_historical_daily_multi(
            daily_symbols, target - timedelta(days=DAILY_DAYS), target, progress=progress)
        five = feed.get_historical_bars_multi(
            symbols, "5Min", target - timedelta(days=INTRADAY_DAYS * 2 + 5), target,
            progress=progress)
        missing_daily = sorted(set(daily_symbols) - set(daily))
        missing_5m = sorted(set(symbols) - set(five))
        result = {
            "status": "ok" if not missing_daily and not missing_5m else "partial",
            "session": target.isoformat(), "completed_at": datetime.now(timezone.utc).isoformat(),
            "universe": str(path), "active_watchlist": active.name if active else None,
            "symbols": len(symbols), "daily": len(daily), "five_minute": len(five),
            "missing_daily": missing_daily, "missing_5m": missing_5m,
            "cache": feed._history_stats,
        }
        _write_result(result_path, result)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="REST-only after-close Schwab history refresh")
    parser.add_argument("--session", type=date.fromisoformat, help="NYSE session date; default today ET")
    parser.add_argument("--universe", type=Path, help="Universe CSV; default matches start_scanner.bat")
    parser.add_argument("--dry-run", action="store_true", help="Show target/universe without auth or network")
    parser.add_argument("--live-port", type=int, default=7777, help="Live scanner API port guard")
    args = parser.parse_args()
    try:
        result = refresh(session=args.session, universe=args.universe,
                         dry_run=args.dry_run, live_port=args.live_port)
    except Exception as exc:
        parser.exit(2, f"Schwab history refresh failed: {exc}\n")
    print(json.dumps(result, indent=2), flush=True)
    if result["status"] == "partial":
        parser.exit(1, "Some symbols could not be refreshed; next run will retry missing coverage.\n")


if __name__ == "__main__":
    main()
