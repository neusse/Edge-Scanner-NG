"""Versioned alert identity, recovery, status, and schema compatibility."""
import asyncio
import json
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
from starlette.websockets import WebSocketDisconnect

from scanner.api import AppState, create_app
from scanner.feed_hub import FeedHub, Subscription
from tests.test_api_v2 import FakeScanner, _states


SCHEMA = json.loads((Path(__file__).parent.parent / "docs/schemas/alert-feed-v1.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())


def _alert(symbol="AAPL", **extra):
    return {"symbol": symbol, "setup": "x", "direction": "long", "price": 100.0,
            "trigger": "hod", "timestamp": "2026-09-23T10:31:00-04:00", **extra}


def _app(hub):
    return create_app(AppState(scanner=FakeScanner(_states()), feed=None, hub=hub))


def test_documented_fixtures_validate_and_replay_mode_is_distinct():
    examples = json.loads((Path(__file__).parent / "fixtures/alert_feed_v1.json").read_text())
    assert {"system", "custom", "exit_watch", "replay"} <= examples.keys()
    for frame in examples.values():
        VALIDATOR.validate(frame)
    assert examples["replay"]["mode"] == "replay"
    assert examples["exit_watch"]["alert"]["direction"] == "long"


def test_live_frames_validate_and_ids_survive_restart_for_recovery(tmp_path):
    store = tmp_path / "all"
    first = FeedHub(store_dir=store)
    a = first.publish(_alert(), "custom")
    assert a["archive_write_ok"] is True
    restarted = FeedHub(store_dir=store, load_persisted=True)
    b = restarted.publish(_alert("MSFT"), "custom")
    assert a["seq"] == b["seq"] == 1  # process-local only
    assert a["event_id"] != b["event_id"]
    page = restarted.recover_for(Subscription(), a["event_id"])
    assert [x["event_id"] for x in page["alerts"]] == [b["event_id"]]
    assert page["next_cursor"] == b["event_id"] and not page["has_more"]
    with TestClient(_app(restarted)) as client:
        with client.websocket_connect("/ws/alerts") as ws:
            replay = ws.receive_json()
            VALIDATOR.validate(replay)
            assert [x["event_id"] for x in replay["alerts"]] == [b["event_id"], a["event_id"]]
            restarted.publish(_alert("NVDA"), "custom")
            frame = ws.receive_json()
            VALIDATOR.validate(frame)
            assert frame["type"] == "alert" and frame["event_id"] == frame["alert"]["event_id"]


def test_recovery_is_oldest_first_paged_and_expired_cursor_is_explicit(tmp_path):
    hub = FeedHub(store_dir=tmp_path / "all")
    alerts = [hub.publish(_alert(symbol), "custom") for symbol in ("AAPL", "MSFT", "NVDA")]
    page = hub.recover_for(Subscription(), None, limit=2)
    assert [a["symbol"] for a in page["alerts"]] == ["AAPL", "MSFT"]
    assert page["has_more"] and page["next_cursor"] == alerts[1]["event_id"]
    assert [a["symbol"] for a in hub.recover_for(Subscription(), page["next_cursor"])["alerts"]] == ["NVDA"]
    with TestClient(_app(hub)) as client:
        response = client.get("/api/alerts/recover?after=missing")
        assert response.status_code == 410 and response.json()["error"] == "cursor_not_found"
        assert client.get("/api/alerts?direction=sideways").status_code == 400
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/ws/alerts?direction=sideways"):
                pass
        assert exc.value.code == 1008


def test_legacy_archive_identity_is_stable_and_not_marked_live(tmp_path):
    store = tmp_path / "all"
    store.mkdir()
    path = store / f"{date.today().isoformat()}.jsonl"
    path.write_text(json.dumps(_alert()) + "\n", encoding="utf-8")
    a = FeedHub(store_dir=store, load_persisted=True).recent[0]
    b = FeedHub(store_dir=store, load_persisted=True).recent[0]
    assert a["event_id"] == b["event_id"]
    assert a["mode"] == "unknown" and a["archive_write_ok"] is True


def test_replay_mode_alerts_and_heartbeat_status_are_explicit(tmp_path, monkeypatch):
    from scanner import feed_hub
    monkeypatch.setattr(feed_hub, "HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(feed_hub, "MARKET_STALE_SECONDS", 0.05)

    async def run():
        hub = FeedHub(store_dir=tmp_path, mode="replay")
        frames = []
        async def send(message):
            frames.append(json.loads(message))
        cid = hub.add_client(send, Subscription(), replay=True)
        hub._ensure_sender(cid, hub._clients[cid])
        hub.record_market_bar("2026-09-23T14:31:00+00:00")
        hub.publish(_alert(), "custom")
        task = asyncio.create_task(hub.broadcast_loop())
        await asyncio.sleep(0.18)
        task.cancel()
        return frames

    frames = asyncio.run(run())
    for frame in frames:
        VALIDATOR.validate(frame)
    assert frames[0]["type"] == "replay"
    assert any(f["type"] == "alert" and f["mode"] == "replay" for f in frames)
    assert any(f["type"] == "heartbeat" and f["status"]["state"] == "stale" for f in frames)


def test_invalid_archive_write_is_visible_and_not_recoverable(tmp_path, monkeypatch):
    hub = FeedHub(store_dir=tmp_path)
    monkeypatch.setattr(hub.store, "save", lambda _alert: False)
    alert = hub.publish(_alert(), "custom")
    assert alert["archive_write_ok"] is False
    with pytest.raises(LookupError):
        hub.recover_for(Subscription(), alert["event_id"])
