"""Regressions for the defects the audit-2 verification pass found in the first
round of fixes: burst fan-out, replay ownership, the IPv6 bind fallback, and
late prior-date bars."""
import asyncio
import json
import socket

import pandas as pd
import pytest

from scanner import feed_hub
from scanner.feed_hub import FeedHub, Subscription


def _alert(i: int) -> dict:
    return {"symbol": "AAPL", "setup": "x", "direction": "long", "price": 1.0 + i}


# ── V1: a burst above the outbox size must not drop a healthy client ─────────

def test_burst_above_outbox_keeps_fast_client_and_drops_blocked_one(tmp_path):
    async def run():
        hub = FeedHub(store_dir=tmp_path)
        got, closed = [], []
        never = asyncio.Event()

        async def fast(msg):
            got.append(msg)

        async def blocked(msg):
            await never.wait()

        async def close():
            closed.append(1)

        sub = Subscription.from_params({})
        hub.add_client(blocked, sub, "stuck", close=close)
        hub.add_client(fast, sub, "ok")
        for i in range(feed_hub._CLIENT_QUEUE + 50):          # before the first broadcast tick
            hub.publish(_alert(i), "custom")
        loop = asyncio.create_task(hub.broadcast_loop())
        await asyncio.sleep(1.0)
        loop.cancel()
        return hub, got, closed

    hub, got, closed = asyncio.run(run())
    assert len(got) == feed_hub._CLIENT_QUEUE + 50            # every alert, in order
    seqs = [json.loads(m)["alert"]["seq"] for m in got]
    assert seqs == sorted(seqs)
    assert [c["name"] for c in hub.clients()] == ["ok"]       # only the blocked one was dropped
    assert closed == [1]


# ── R1: replay and live never overlap, and one sender owns the socket ────────

def test_replay_cutoff_is_exactly_once_and_replay_is_sent_first(tmp_path):
    async def run():
        hub = FeedHub(store_dir=tmp_path)
        frames = []

        async def send(msg):
            frames.append(json.loads(msg))

        hub.publish(_alert(1), "custom")                      # seq 1: before registration
        cid = hub.add_client(send, Subscription.from_params({}), "c", replay=True)
        hub.publish(_alert(2), "custom")                      # seq 2: after registration, before any send
        hub._ensure_sender(cid, hub._clients[cid])
        loop = asyncio.create_task(hub.broadcast_loop())
        await asyncio.sleep(0.3)
        loop.cancel()
        return frames

    frames = asyncio.run(run())
    assert [f["type"] for f in frames] == ["replay", "alert"]          # replay first, through the same writer
    assert [a["seq"] for a in frames[0]["alerts"]] == [1]
    assert frames[1]["alert"]["seq"] == 2                              # seq 2 exactly once
    assert frames[0]["truncated"] is False


def test_replay_says_when_it_is_truncated(tmp_path):
    hub = FeedHub(store_dir=tmp_path)
    for i in range(feed_hub._REPLAY_LIMIT + 5):
        hub.publish(_alert(i), "custom")
    cid = hub.add_client(None, Subscription.from_params({}), "c", replay=True)
    frame = json.loads(hub._clients[cid].outbox.get_nowait())
    assert frame["truncated"] is True and len(frame["alerts"]) == feed_hub._REPLAY_LIMIT


# ── V3: a taken IPv6 port is an error, not "IPv6 is disabled" ────────────────

def test_bind_fails_when_ipv6_port_is_taken():
    from scanner.api import bind_sockets
    try:
        taken = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
        taken.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        taken.bind(("::1", 0))
    except OSError:
        pytest.skip("no IPv6 loopback on this machine")
    taken.listen(1)
    port = taken.getsockname()[1]
    try:
        with pytest.raises(OSError):
            bind_sockets("127.0.0.1", port)
        # and the IPv4 listener it had already opened was closed again
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.bind(("127.0.0.1", port))
        probe.close()
    finally:
        taken.close()


def test_bind_default_is_both_loopbacks():
    from scanner.api import bind_sockets
    socks = bind_sockets("127.0.0.1", 0)
    try:
        assert socks[0].getsockname()[0] == "127.0.0.1"
        assert all(s.getsockname()[0] in ("127.0.0.1", "::1") for s in socks)
    finally:
        for s in socks:
            s.close()


# ── V4: a late bar from an earlier date never reaches today's state ──────────

def _bar(ts_et: str, price: float, sym: str = "AAPL") -> dict:
    ts = pd.Timestamp(ts_et, tz="America/New_York").tz_convert("UTC")
    return {"symbol": sym, "timestamp": ts, "open": price, "high": price, "low": price,
            "close": price, "volume": 100.0}


def test_prior_date_bar_is_dropped_before_any_mutation():
    from scanner.live_scanner import LiveScanner
    sc = LiveScanner.__new__(LiveScanner)
    seen = []
    sc._spy_state = type("S", (), {"on_bar": lambda self, b: seen.append(b), "vwap": None})()
    sc._states = {}
    sc.reset_session = lambda: None
    sc._on_bar(_bar("2026-09-22 09:30", 400.0, "SPY"))
    sc._on_bar(_bar("2026-09-21 15:59", 10.0, "SPY"))         # late, from yesterday
    sc._on_bar(_bar("2026-09-22 09:31", 401.0, "SPY"))
    assert [b["close"] for b in seen] == [400.0, 401.0]


# ── premarket lists on a provider with no batch history (Schwab) ─────────────

def test_premarket_lists_come_from_live_state_with_no_requests():
    from types import SimpleNamespace
    from fastapi.testclient import TestClient
    from scanner.api import AppState, create_app

    calls = []
    feed = SimpleNamespace(per_symbol_history=True,
                           get_todays_bars_multi=lambda *a, **k: calls.append(1) or {})
    states = {
        "UP": SimpleNamespace(prior_close=10.0, pm_last=11.0, pm_vol=5_000.0),
        "DOWN": SimpleNamespace(prior_close=20.0, pm_last=19.0, pm_vol=9_000.0),
        "QUIET": SimpleNamespace(prior_close=5.0, pm_last=None, pm_vol=0.0),
    }
    app_state = AppState(scanner=SimpleNamespace(_states=states, _profiles=None), feed=feed)
    r = TestClient(create_app(app_state), base_url="http://localhost:7777").get("/api/premarket").json()
    assert calls == []                                            # nothing was downloaded
    assert [x["symbol"] for x in r["gainers"]] == ["UP"] and r["gainers"][0]["change_pct"] == 10.0
    assert [x["symbol"] for x in r["losers"]] == ["DOWN"]
    assert [x["symbol"] for x in r["volume"]] == ["DOWN", "UP"]
    assert r["symbols_active"] == 2 and r["symbols_total"] == 3
