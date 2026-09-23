"""The published example consumer fails closed on gaps and never routes orders."""
import io
import json

import pytest

from scripts.consume_alerts import AlertConsumer, RecoveryGap


def _alert(seq, *, mode="live", session="s"):
    return {"schema_version": 1, "event_id": f"{session}:{seq}",
            "session_id": session, "seq": seq, "mode": mode,
            "archive_write_ok": True, "symbol": "AAPL", "setup": "x",
            "direction": "long", "price": 100.0}


def test_replay_is_oldest_first_and_replay_mode_is_not_live(tmp_path):
    consumer = AlertConsumer("ws://localhost:7777/ws/alerts", tmp_path / "cursor")
    consumer.process_frame({"schema_version": 1, "type": "replay", "truncated": False,
                            "alerts": [_alert(3), _alert(2), _alert(1)]})
    assert consumer.cursor == "s:3"
    assert consumer.cursor_file.read_text().strip() == "s:3"
    assert consumer.accept(_alert(4, mode="replay")) is False
    assert consumer.cursor == "s:4"  # tracked, but never eligible for execution


def test_reconnect_recovery_prevents_replay_from_moving_cursor_backward(tmp_path, monkeypatch):
    consumer = AlertConsumer("ws://localhost:7777/ws/alerts", tmp_path / "cursor")
    consumer.accept(_alert(3))
    calls = []
    monkeypatch.setattr(consumer, "recover", lambda: calls.append(1))
    consumer.process_frame({"schema_version": 1, "type": "replay", "truncated": False,
                            "alerts": [_alert(3), _alert(2), _alert(1)]})
    assert calls == [1] and consumer.cursor == "s:3"


def test_seq_gap_triggers_archive_recovery_for_unfiltered_feed(tmp_path, monkeypatch):
    consumer = AlertConsumer("ws://localhost:7777/ws/alerts", tmp_path / "cursor")
    consumer.accept(_alert(1))
    monkeypatch.setattr(consumer, "recover", lambda: consumer.accept(_alert(2)))
    consumer.process_frame({"schema_version": 1, "type": "alert", "alert": _alert(3)})
    assert consumer.cursor == "s:3" and consumer.last_seq == 3


def test_archive_recovery_pages_and_expired_cursor_is_fatal(tmp_path, monkeypatch):
    from scripts import consume_alerts
    consumer = AlertConsumer("ws://localhost:7777/ws/alerts?symbols=AAPL", tmp_path / "cursor")
    pages = [
        {"alerts": [_alert(1), _alert(2)], "has_more": True},
        {"alerts": [_alert(3)], "has_more": False},
    ]
    urls = []

    def fetch(url, timeout):
        urls.append(url)
        return io.BytesIO(json.dumps(pages.pop(0)).encode())

    monkeypatch.setattr(consume_alerts, "urlopen", fetch)
    assert consumer.recover() == 3
    assert consumer.cursor == "s:3" and "symbols=AAPL" in urls[0] and "after=s%3A2" in urls[1]

    def expired(url, timeout):
        from urllib.error import HTTPError
        raise HTTPError(url, 410, "expired", {}, None)

    monkeypatch.setattr(consume_alerts, "urlopen", expired)
    with pytest.raises(RecoveryGap, match="expired"):
        consumer.recover()
