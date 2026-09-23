"""Unified alert feed: subscription filters, sink tap, WebSocket replay + push, REST."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from scanner.alert_sink import AlertSink
from scanner.api import AppState, create_app
from scanner.feed_hub import FeedHub, Subscription, sanitize
from tests.test_api_v2 import FakeScanner, _states


def _alert(**kw) -> dict:
    a = {"symbol": "AAPL", "direction": "long", "timestamp": "2026-09-08T14:31:00-04:00", "price": 100.0,
         "trigger": "SYS_X2", "triggers_fired": [], "score": 80, "market_regime": "neutral", "conditions": {}}
    a.update(kw)
    return a


# ── filters ──────────────────────────────────────────────────────────────────

def test_subscription_from_params_and_matching():
    sub = Subscription.from_params({"sources": "system", "setups": "X1,X2, X2_LITE", "min_score": "70"})
    assert sub.sources == {"system"} and sub.setups == {"X1", "X2", "X2_LITE"} and sub.min_score == 70
    assert sub.matches({"source": "system", "setup": "X1", "score": 75})
    assert not sub.matches({"source": "system", "setup": "X3", "score": 75})
    assert not sub.matches({"source": "custom", "setup": "X1", "score": 75})
    assert not sub.matches({"source": "system", "setup": "X1", "score": 60})
    # 'all' / empty means no restriction
    assert Subscription.from_params({"sources": "all"}).sources == frozenset()
    assert Subscription.from_params({}).matches({"source": "custom", "setup": "cs_x"})
    # custom flag both ways, triggers match trigger or entry_trigger, symbols upper-cased
    assert not Subscription.from_params({"custom": "false"}).matches({"source": "custom", "custom": True})
    assert Subscription.from_params({"custom": "only"}).matches({"source": "custom", "custom": True})
    t = Subscription.from_params({"triggers": "power_bar,hod:high"})
    assert t.matches({"trigger": "power_bar"}) and t.matches({"trigger": "CS_x:hod:high", "entry_trigger": "hod:high"})
    assert not t.matches({"trigger": "orb"})
    assert Subscription.from_params({"symbols": "aapl"}).matches({"symbol": "AAPL"})
    assert Subscription.from_params({"direction": "short"}).matches({"direction": "short"})
    with pytest.raises(ValueError, match="invalid direction"):
        Subscription.from_params({"direction": "sideways"})


def test_sanitize_nan_and_numpy():
    import numpy as np
    out = sanitize({"a": float("nan"), "b": np.float64(2.5), "c": [np.int64(1)], "d": True})
    assert out == {"a": None, "b": 2.5, "c": [1], "d": True}


# ── hub ──────────────────────────────────────────────────────────────────────

def test_tap_publishes_only_accepted_alerts_and_tags_source(tmp_path: Path):
    hub = FeedHub(store_dir=tmp_path / "all", keep_days=1)
    inner = AlertSink(cooldown_minutes=5)
    sink = hub.tap(inner, "system")
    assert sink.push(_alert(setup="X2")) is True
    assert sink.push(_alert(setup="X2")) is False              # inner cooldown rejects, hub stays quiet
    assert sink.push(_alert(symbol="NVDA", setup="cs_up", custom=True, trigger="CS_cs_up:hod:high")) is True
    assert len(inner) == 2 and len(sink) == 2                 # delegates to the legacy sink
    recent = hub.recent_for(Subscription())
    assert [a["source"] for a in recent] == ["custom", "system"]  # newest first, custom tagged from the flag
    assert hub.recent_for(Subscription.from_params({"sources": "system"}))[0]["symbol"] == "AAPL"
    # archived with the source field
    lines = [json.loads(l) for p in (tmp_path / "all").glob("*.jsonl") for l in p.read_text().splitlines()]
    assert {l["source"] for l in lines} == {"system", "custom"}
    with pytest.raises(ValueError):
        hub.tap(inner, "bogus")


def test_tap_restores_restart_cooldown_from_persisted_archive(tmp_path: Path):
    store_dir = tmp_path / "all"
    first_hub = FeedHub(store_dir=store_dir, keep_days=1)
    first_sink = first_hub.tap(AlertSink(cooldown_minutes=5), "system")
    assert first_sink.push(_alert(timestamp="2026-09-08T10:30:00-04:00")) is True

    restarted_hub = FeedHub(store_dir=store_dir, keep_days=1, load_persisted=True)
    restarted_sink = restarted_hub.tap(AlertSink(cooldown_minutes=5), "system")

    assert restarted_sink.push(_alert(timestamp="2026-09-08T10:33:00-04:00")) is False
    assert restarted_sink.push(_alert(timestamp="2026-09-08T10:36:00-04:00")) is True
    assert restarted_sink.push(_alert(timestamp="2026-09-09T09:30:00-04:00")) is True


# ── API ──────────────────────────────────────────────────────────────────────

@pytest.fixture
def app(tmp_path: Path):
    hub = FeedHub(store_dir=tmp_path / "all", keep_days=1)
    app_state = AppState(scanner=FakeScanner(_states()), feed=None, hub=hub)  # type: ignore[arg-type]
    return create_app(app_state), hub


def test_ws_replay_is_filtered_and_pushes_matching_alerts(app):
    fastapi_app, hub = app
    cs = hub.tap(AlertSink(), "custom")
    sys_ = hub.tap(AlertSink(), "system")
    cs.push(_alert(trigger="power_bar", symbol="MSFT"))
    sys_.push(_alert(setup="X1", trigger="SYS_X1"))
    sys_.push(_alert(setup="X3", trigger="SYS_X3", symbol="TSLA", direction="short"))
    with TestClient(fastapi_app) as c:
        with c.websocket_connect("/ws/alerts?sources=system&setups=X1,X2") as ws:
            replay = ws.receive_json()
            assert replay["type"] == "replay" and [a["setup"] for a in replay["alerts"]] == ["X1"]
            assert replay["filter"]["setups"] == ["X1", "X2"]
            sys_.push(_alert(setup="X2", trigger="SYS_X2", symbol="AMD", timestamp="2026-09-08T14:40:00-04:00"))
            sys_.push(_alert(setup="X3", trigger="SYS_X3", symbol="XOM", timestamp="2026-09-08T14:41:00-04:00"))
            cs.push(_alert(trigger="orb", symbol="NFLX", timestamp="2026-09-08T14:42:00-04:00"))
            msg = ws.receive_json()
            assert msg["type"] == "alert" and msg["alert"]["setup"] == "X2" and msg["alert"]["source"] == "system"
        with c.websocket_connect("/ws/alerts") as ws:
            replay = ws.receive_json()
            assert [a["source"] for a in replay["alerts"]] == ["custom", "system", "system", "system", "system", "custom"]
        r = c.get("/api/alerts?sources=custom").json()
        assert [a["trigger"] for a in r["alerts"]] == ["orb", "power_bar"] and r["filter"]["sources"] == ["custom"]
        assert c.get("/api/alerts?symbols=tsla").json()["alerts"][0]["symbol"] == "TSLA"
        assert c.get("/api/feed/clients").json()["published"] == 6
