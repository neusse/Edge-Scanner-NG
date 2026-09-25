"""Frozen replay input, clock controls, and isolation from the live feed."""
from __future__ import annotations

import json
import socket
import subprocess
import sys
import threading
from datetime import date
from pathlib import Path

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from scanner.api import AppState, create_app
from scanner.live_scanner import LiveScanner
from scanner.replay import ReplayController, ReplayFeed
from scanner.replay_input import ReplayInput
from tests.test_live_scanner import _warmup


class HistoryOnly:
    def get_bars_range(self, symbol, timeframe, start, end):
        if timeframe == "Day":
            stamps = pd.DatetimeIndex(["2026-09-21"], tz="UTC")
        elif timeframe == "5Min":
            stamps = pd.DatetimeIndex(["2026-09-21T14:30:00Z"])
        else:
            stamps = pd.DatetimeIndex(["2026-09-22T13:30:00Z", "2026-09-22T13:31:00Z"])
        return pd.DataFrame({"open": [100.0] * len(stamps), "high": [101.0] * len(stamps),
                             "low": [99.0] * len(stamps), "close": [100.5] * len(stamps),
                             "volume": [1000.0] * len(stamps)}, index=stamps)


def test_capture_is_immutable_and_spy_precedes_stock(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot = ReplayInput.capture(HistoryOnly(), date(2026, 9, 22), ["AAPL"], {}, base=tmp_path / "inputs")
    bars = snapshot.ordered_bars()
    assert [b["symbol"] for b in bars] == ["SPY", "AAPL", "SPY", "AAPL"]
    assert bars[0]["timestamp"] == bars[1]["timestamp"]
    assert len(snapshot.ordered_bars("09:30")) == 2
    assert (snapshot.root / "config/universe.csv").read_text(encoding="utf-8").splitlines() == ["symbol", "AAPL"]
    assert ReplayInput.load(snapshot.root).data_id == snapshot.data_id
    assert snapshot.frame("AAPL", "daily").index.max().date() < snapshot.date
    with (snapshot.root / "bars/AAPL-1min.parquet").open("ab") as handle:
        handle.write(b"tampered")
    with pytest.raises(ValueError, match="changed"):
        ReplayInput.load(snapshot.root)


def test_replay_feed_has_no_stream_or_network_chart_fetch(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot = ReplayInput.capture(HistoryOnly(), date(2026, 9, 22), ["AAPL"], {}, base=tmp_path / "inputs")
    feed = ReplayFeed(snapshot)
    with pytest.raises(RuntimeError, match="stream"):
        feed.subscribe_minute_bars(["AAPL"], lambda _: None)
    with pytest.raises(RuntimeError, match="future bars"):
        feed.get_bars_range("AAPL", "1Min", snapshot.date, snapshot.date)


def test_controller_step_reset_and_replay_sequence():
    bars = [{"timestamp": pd.Timestamp("2026-09-22T13:30:00Z"), "symbol": "SPY"},
            {"timestamp": pd.Timestamp("2026-09-22T13:30:00Z"), "symbol": "AAPL"},
            {"timestamp": pd.Timestamp("2026-09-22T13:31:00Z"), "symbol": "SPY"}]
    seen = []
    resets = []
    ctl = ReplayController(bars, lambda group: seen.append([b["symbol"] for b in group]),
                           lambda: resets.append(True), paused=True, speed=0)
    thread = threading.Thread(target=ctl.run, daemon=True)
    thread.start()
    try:
        ctl.control("step")
        for _ in range(100):
            if ctl.status()["position"] == 1:
                break
            threading.Event().wait(0.01)
        assert seen == [["SPY", "AAPL"]]
        assert ctl.status()["paused"] is True
        ctl.control("reset")
        assert resets == [True]
        assert ctl.status()["position"] == 0
        ctl.control("resume")
        for _ in range(100):
            if ctl.status()["complete"]:
                break
            threading.Event().wait(0.01)
        assert seen[1:] == [["SPY", "AAPL"], ["SPY"]]
    finally:
        ctl.control("stop")
        thread.join(timeout=2)
    assert not thread.is_alive()


def test_replay_api_freezes_config_and_charts_show_processed_bars_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot = ReplayInput.capture(HistoryOnly(), date(2026, 9, 22), ["AAPL"], {}, base=tmp_path / "inputs")
    feed = ReplayFeed(snapshot)
    scanner = LiveScanner(["AAPL"], feed)
    _warmup(scanner)
    app_state = AppState(scanner, feed)
    app_state.replay = {"date": "2026-09-22", "cursor": None, "speed": 1}
    app_state.replay_input = snapshot
    app_state.replay_controller = ReplayController([], lambda _: None, lambda: None)
    client = TestClient(create_app(app_state))
    assert client.post("/api/v2/settings", json={}).status_code == 403
    assert client.get("/api/bars/AAPL/1min").json()["bars"] == []
    assert client.get("/api/replay/status").json()["date"] == "2026-09-22"
    bar = snapshot.ordered_bars()[1]
    scanner._on_bar(bar)
    shown = client.get("/api/bars/AAPL/1min").json()["bars"]
    assert len(shown) == 1 and shown[0]["t"][11:16] == "09:30"


def test_replay_launcher_completes_without_a_stream(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot = ReplayInput.capture(HistoryOnly(), date(2026, 9, 22), ["AAPL"], {}, base=tmp_path / "inputs")

    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    project = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(project / "scripts/run_replay.py"), "--input", str(snapshot.root),
         "--live-port", str(free_port()), "--port", str(free_port()),
         "--runs-dir", str(tmp_path / "runs"),
         "--speed", "0", "--exit-on-complete"],
        cwd=project, capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Replay pass 1 complete" in result.stdout
    summaries = [json.loads(path.read_text(encoding="utf-8"))
                 for path in (tmp_path / "runs").glob("2026-09-22-*/pass-1/summary.json")]
    summary = next(s for s in summaries if s["input"] == str(snapshot.root))
    assert summary["bars"] == 2
    assert summary["data_id"] == snapshot.data_id


def test_replay_emits_mode_tagged_alert_only_to_its_own_archive(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    setup_dir = tmp_path / "data/setups/custom"
    setup_dir.mkdir(parents=True)
    (setup_dir / "test_hod.json").write_text(json.dumps({
        "id": "test_hod", "name": "Replay HOD", "enabled": True,
        "mode": "or", "direction": "long", "sessions": ["rth"],
        "triggers": [{"id": "hod", "options": ["high"]}],
    }), encoding="utf-8")

    class RisingHistory(HistoryOnly):
        def get_bars_range(self, symbol, timeframe, start, end):
            frame = super().get_bars_range(symbol, timeframe, start, end)
            if timeframe == "1Min" and symbol == "AAPL":
                frame.loc[frame.index[-1], "high"] = 103.0
                frame.loc[frame.index[-1], "close"] = 102.0
            return frame

    snapshot = ReplayInput.capture(RisingHistory(), date(2026, 9, 22), ["AAPL"], {}, base=tmp_path / "inputs")

    def free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    project = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        [sys.executable, str(project / "scripts/run_replay.py"), "--input", str(snapshot.root),
         "--live-port", str(free_port()), "--port", str(free_port()),
         "--runs-dir", str(tmp_path / "runs"), "--speed", "0", "--exit-on-complete"],
        cwd=project, capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    run = next((tmp_path / "runs").glob("*/pass-1"))
    alerts = [json.loads(line) for path in (run / "alerts/all").glob("*.jsonl")
              for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(alerts) == 1
    assert alerts[0]["mode"] == "replay"
    assert alerts[0]["setup"] == "test_hod"
    assert not (tmp_path / "data/alerts/all").exists()


def test_replay_refuses_an_existing_live_listener_before_writing_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    snapshot = ReplayInput.capture(HistoryOnly(), date(2026, 9, 22), ["AAPL"], {}, base=tmp_path / "inputs")
    project = Path(__file__).resolve().parent.parent
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        live_port = sock.getsockname()[1]
        result = subprocess.run(
            [sys.executable, str(project / "scripts/run_replay.py"), "--input", str(snapshot.root),
             "--live-port", str(live_port), "--runs-dir", str(tmp_path / "runs")],
            cwd=project, capture_output=True, text=True, timeout=15,
        )
    assert result.returncode == 2
    assert "stop it before replay" in result.stderr
    assert not (tmp_path / "runs").exists()
