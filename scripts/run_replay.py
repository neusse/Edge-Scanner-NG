"""Run the production scanner against an immutable historical input bundle.

No streaming connection is created. The live scanner and replay share an
installation lock; an older live process is also detected by its API port.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sys
import threading
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

import pandas as pd

from scanner.alert_sink import AlertSink
from scanner.api import AppState, bind_sockets, create_app
from scanner.custom_setups import CustomEvaluator, CustomSetupStore
from scanner.data import make_feed
from scanner.events import EventBuffer, make_hodlod_hook
from scanner.feed_hub import FeedHub
from scanner.instance_lock import ScannerInstanceLock
from scanner.live_scanner import LiveScanner
from scanner.profiles import ParamSetStore, ProfileEngine, ProfileStore, SetupProfiles
from scanner.replay import ReplayController, ReplayFeed
from scanner.replay_input import ReplayInput
from scanner.settings import settings

ET = ZoneInfo("America/New_York")
FUNDAMENTAL_CONDITIONS = {"market_cap", "float_shares", "short_pct_float"}


def _engine_id() -> str:
    digest = hashlib.sha256()
    for path in sorted((ROOT / "scanner").rglob("*.py")) + [Path(__file__)]:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def _live_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.4):
            return True
    except OSError:
        return False


def _symbols(path: Path) -> list[str]:
    import csv
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.DictReader(handle)
        if not rows.fieldnames or "symbol" not in rows.fieldnames:
            raise ValueError(f"{path}: expected a symbol column")
        return sorted({row["symbol"].strip().upper() for row in rows if row.get("symbol", "").strip()})


def _sector_map(path: Path) -> dict[str, str]:
    import csv
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return {row["symbol"].strip().upper(): row["sector_etf"].strip().upper()
                for row in csv.DictReader(handle) if row.get("symbol") and row.get("sector_etf")}


def _static_fundamental_profiles(engine: ProfileEngine) -> list[str]:
    bad = []
    for profile in engine.store.load_all():
        if any(c.get("field") in FUNDAMENTAL_CONDITIONS or c.get("id") in FUNDAMENTAL_CONDITIONS
               for c in profile.get("conditions", [])):
            bad.append(profile["id"])
    return bad


def _build_scanner(snapshot: ReplayInput, feed: ReplayFeed, hub: FeedHub,
                   app_state: AppState, first_alert: pd.Timestamp) -> tuple[LiveScanner, object]:
    scanner = app_state.scanner
    symbol_daily = {s: snapshot.frame(s, "daily") for s in snapshot.symbols}
    bars_5m = {s: snapshot.frame(s, "5min") for s in snapshot.symbols}
    sectors = {s: snapshot.frame(s, "daily") for s in set(snapshot.sector_map.values())}
    scanner.warmup(snapshot.frame("SPY", "daily"), symbol_daily,
                   sector_daily=sectors, bars_5m=bars_5m, session_date=snapshot.date)
    config = snapshot.root / "config"
    no_defaults = snapshot.root / "config/no-defaults.json"
    evaluator = CustomEvaluator(CustomSetupStore(config / "setups/custom", defaults=no_defaults))
    if evaluator.plan.trade_keys:
        raise ValueError("this snapshot enables orb_trade_cross, but no historical trade events were captured")
    scanner.attach_custom(evaluator)
    evaluator.warmup(symbol_daily, bars_5m)
    # Feed earlier bars to every evaluator and cooldown, but do not expose
    # their alerts when the requested display interval starts later.
    base_sink = AlertSink()
    tapped_sink = hub.tap(base_sink, "custom")

    class WindowedSink:
        def push(self, alert: dict) -> bool:
            if pd.Timestamp(alert["timestamp"]) < first_alert:
                return base_sink.push(alert)
            return tapped_sink.push(alert)

    scanner.attach_custom(evaluator, WindowedSink())
    app_state.custom_eval = evaluator
    profiles = ProfileEngine(
        store=ProfileStore(config / "universe/profiles", defaults=no_defaults),
        assignments=SetupProfiles(config / "setups/profiles.json"),
        param_sets=ParamSetStore(config / "setups/paramsets", defaults=no_defaults),
    )
    if bad := _static_fundamental_profiles(profiles):
        raise ValueError("historical/as-of fundamentals are unavailable; replay cannot use profiles: " + ", ".join(bad))
    scanner.attach_profiles(profiles)
    profiles.resolve_members(scanner._states, {})
    app_state.event_buffer = EventBuffer()
    if hasattr(app_state, "v2"):
        app_state.v2.events = app_state.event_buffer
    return scanner, make_hodlod_hook(scanner, app_state.event_buffer)


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline, isolated Edge alert replay")
    parser.add_argument("--date", type=date.fromisoformat, help="Historical session (YYYY-MM-DD); required for a new capture")
    parser.add_argument("--input", type=Path, help="Reuse an immutable input bundle (no network access)")
    parser.add_argument("--universe", type=Path, default=Path("data/universe.csv"))
    parser.add_argument("--symbols", help="Comma-separated symbols instead of the universe file")
    parser.add_argument("--start", default="04:00", help="First alert time in New York (default 04:00)")
    parser.add_argument("--end", default="20:00", help="Last bar time in New York (default 20:00)")
    parser.add_argument("--speed", type=float, default=60.0, help="Clock speed; 0 is fastest (default 60)")
    parser.add_argument("--paused", action="store_true", help="Start paused")
    parser.add_argument("--port", type=int, default=7778, help="Replay dashboard port (default 7778)")
    parser.add_argument("--runs-dir", type=Path, default=Path("data/replay/runs"),
                        help="Folder for isolated replay outputs")
    parser.add_argument("--live-port", type=int, default=7777, help="Live API port to guard (default 7777)")
    parser.add_argument("--exit-on-complete", action="store_true", help="Exit when playback ends")
    args = parser.parse_args()
    if bool(args.date) == bool(args.input):
        parser.error("choose exactly one of --date or --input")
    if args.speed < 0 or not ("00:00" <= args.start <= args.end <= "23:59"):
        parser.error("invalid speed or start/end time")

    lock = ScannerInstanceLock(Path("data/.scanner-instance.lock"), "replay")
    try:
        lock.acquire()
    except RuntimeError as exc:
        parser.exit(2, f"{exc}\n")
    try:
        if _live_port_open(args.live_port):
            parser.exit(2, f"Live scanner is listening on port {args.live_port}; stop it before replay.\n")
        # Bind early, before a long historical capture, so replay never runs
        # invisibly because its dashboard port was occupied.
        socks = bind_sockets("127.0.0.1", args.port)
        try:
            if args.input:
                snapshot = ReplayInput.load(args.input)
            else:
                symbols = ([s.strip().upper() for s in args.symbols.split(",") if s.strip()]
                           if args.symbols else _symbols(args.universe))
                sector_map = _sector_map(Path("data/sector_map.csv"))
                from dotenv import load_dotenv
                load_dotenv(ROOT / ".env", override=True)
                if not os.environ.get("SCHWAB_APP_KEY") and os.environ.get("SCHWAB_API_KEY"):
                    os.environ["SCHWAB_APP_KEY"] = os.environ["SCHWAB_API_KEY"]
                from scripts.import_schwab_py_token import main as import_shared_token
                import_shared_token()
                feed = make_feed("schwab")
                print(f"Capturing {args.date} for {len(symbols)} symbols through Schwab REST history ...", flush=True)
                snapshot = ReplayInput.capture(feed, args.date, symbols, sector_map)
            print(f"Frozen input: {snapshot.root}  data={snapshot.data_id}  config={snapshot.config_id}", flush=True)
            engine_id = _engine_id()
            bars = snapshot.ordered_bars(args.end)
            if not bars:
                raise ValueError("no bars in the selected replay period")
            first_alert = pd.Timestamp(f"{snapshot.date} {args.start}", tz=ET)
            run_dir = args.runs_dir / f"{snapshot.date}-{uuid4().hex[:12]}"
            run_dir.mkdir(parents=True, exist_ok=False)
            object.__setattr__(settings, "_dir", snapshot.root / "config/settings")
            settings.load()
            replay_feed = ReplayFeed(snapshot)
            pass_number = 0

            def new_pass() -> Path:
                nonlocal pass_number
                pass_number += 1
                path = run_dir / f"pass-{pass_number}"
                path.mkdir()
                return path

            current_pass = new_pass()
            hub = FeedHub(store_dir=current_pass / "alerts/all", keep_days=9999, mode="replay")
            scanner = LiveScanner(list(snapshot.symbols), replay_feed, sector_map=snapshot.sector_map)
            app_state = AppState(scanner=scanner, feed=replay_feed, hub=hub)
            app_state.replay_input = snapshot
            app_state.replay = {"date": snapshot.date.isoformat(), "cursor": None, "speed": args.speed,
                                "data_id": snapshot.data_id, "config_id": snapshot.config_id,
                                "engine_id": engine_id,
                                "run_dir": str(run_dir), "input_dir": str(snapshot.root)}
            scanner, hook = _build_scanner(snapshot, replay_feed, hub, app_state, first_alert)

            def on_group(group: list[dict]) -> None:
                stamp = group[0]["timestamp"]
                # Pre-start bars still advance every production state machine.
                # Only emission is hidden until the selected start time.
                for bar in group:
                    scanner._on_bar(bar)
                    hook(bar)
                hub.record_market_bar(stamp)
                app_state.replay["cursor"] = stamp.isoformat()

            def summary() -> None:
                alerts = hub.store.load_recent()
                payload = {"date": snapshot.date.isoformat(), "input": str(snapshot.root),
                           "data_id": snapshot.data_id, "config_id": snapshot.config_id,
                           "engine_id": engine_id,
                           "pass": pass_number, "bars": controller.status()["position"],
                           "alerts": len(alerts), "by_setup": dict(Counter(a.get("setup") for a in alerts)),
                           "completed_at": datetime.now(timezone.utc).isoformat()}
                (current_pass / "summary.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
                hub.set_status("stopping")
                print(f"Replay pass {pass_number} complete: {len(alerts)} alerts; {current_pass / 'summary.json'}", flush=True)
                if args.exit_on_complete:
                    controller.control("stop")

            def reset() -> None:
                nonlocal scanner, hook, current_pass
                current_pass = new_pass()
                with hub._lock:
                    hub.recent.clear()
                    hub.published = 0
                    hub.session_id = uuid4().hex
                    hub.store = type(hub.store)(store_dir=current_pass / "alerts/all", keep_days=9999)
                    for client in hub._clients.values():
                        client.since = 0
                    while not hub._queue.empty():
                        try:
                            hub._queue.get_nowait()
                        except Exception:
                            break
                scanner.__init__(list(snapshot.symbols), replay_feed, sector_map=snapshot.sector_map)
                scanner, hook = _build_scanner(snapshot, replay_feed, hub, app_state, first_alert)
                app_state.replay["cursor"] = None
                app_state._bar_cache.clear()
                hub.set_status("warming_up")

            controller = ReplayController(bars, on_group, reset, on_complete=summary,
                                          speed=args.speed, paused=args.paused)
            app_state.replay_controller = controller
            import uvicorn
            server = uvicorn.Server(uvicorn.Config(create_app(app_state), host="127.0.0.1",
                                                   port=args.port, log_level="warning"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": socks}, daemon=True)
            thread.start()
            print(f"REPLAY only: http://localhost:{args.port}/v2  (live scanner stopped; no stream)", flush=True)
            print(f"Controls: dashboard top bar; Ctrl+C to stop. Alerts: {run_dir}", flush=True)
            hub.set_status("live")
            try:
                controller.run()
            except KeyboardInterrupt:
                controller.control("stop")
            finally:
                server.should_exit = True
                thread.join(timeout=5)
        finally:
            for sock in socks:
                sock.close()
    finally:
        lock.release()


if __name__ == "__main__":
    main()
