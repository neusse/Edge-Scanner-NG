"""Dashboard V2 backend: stores, events, toplists, news, fundamentals, routes.

All offline (news + yfinance are injected fakes). Uses real SymbolState objects
driven through on_bar so /state, /snapshot and toplists see real numbers.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from scanner.api import AppState, create_app
from scanner.api_v2 import clean, price_of, session_now
from scanner.events import EventBuffer, make_hodlod_hook
from scanner.fundamentals import FundamentalsCache
from scanner.json_store import LayoutStore, UniverseSelectionStore, WatchlistStore, normalize_symbols
from scanner.news import NewsClient
from scanner.state import SymbolState
from scanner.toplists import ToplistEngine, build_rows
from tests.helpers import _daily, _feed, _state


# ── fixtures ─────────────────────────────────────────────────────────────────

class FakeRegime:
    value = "bullish"


class FakeScanner:
    def __init__(self, states: dict[str, SymbolState], spy: SymbolState | None = None) -> None:
        self._states = states
        self._spy_state = spy
        self._regime = FakeRegime()
        self._sector_map = {s: "XLK" for s in states}


class FakeYahooScreener:
    def run(self, request: dict) -> dict:
        return {"mode": request.get("mode", "preset"), "preset": request.get("preset"),
                "label": "Fake results", "rows": [{"symbol": "AAPL", "price": 200.0}],
                "count": 1, "total": 1, "limit": request.get("limit", 100), "cached": False}


def _states() -> dict[str, SymbolState]:
    out: dict[str, SymbolState] = {}
    for sym, prices in (("AAA", [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0]),
                        ("BBB", [100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 94.0]),
                        ("CCC", [100.0] * 7)):
        st = _state(symbol=sym)
        _feed(st, prices, start="09:30", vol=50_000.0)
        out[sym] = st
    return out


@pytest.fixture
def client(tmp_path: Path):
    states = _states()
    spy = _state(symbol="SPY")
    _feed(spy, [450.0, 451.0, 452.0], start="09:30", vol=1_000_000.0)
    scanner = FakeScanner(states, spy)
    app_state = AppState(scanner=scanner, feed=None)  # type: ignore[arg-type]
    fake_news = NewsClient("k", "s", get_fn=lambda url, headers, params, timeout: _Resp(200, {"news": [
        {"id": 1, "headline": "<b>Hello</b> &amp; world", "summary": "<p>sum</p>", "url": "http://x", "source": "benzinga",
         "symbols": ["AAA"], "images": [{"size": "small", "url": "http://img/s"}, {"size": "thumb", "url": "http://img/t"}],
         "created_at": "2026-09-04T13:00:00Z"}]}))
    # A bare app: create_app() would register v2 on the real data/ paths first and
    # FastAPI matches the first registration, hiding these injected fakes.
    from fastapi import FastAPI
    from scanner.api_v2 import mount_v2_static, register_v2_routes
    app = FastAPI()
    register_v2_routes(app, app_state, layouts_dir=tmp_path / "layouts", watchlists_path=tmp_path / "wl.json",
                       universe_selection_path=tmp_path / "selection.json", yahoo_screener=FakeYahooScreener(),
                       fundamentals_path=tmp_path / "fund.json", news_client=fake_news,
                       universe_csv=tmp_path / "nope.csv", sector_csv=tmp_path / "nope2.csv")
    mount_v2_static(app)
    return TestClient(app)


def test_create_app_exposes_v2_routes():
    """The 3-line wiring in scanner/api.py: v2 routes exist on the real app and win over the SPA fallback."""
    app_state = AppState(scanner=FakeScanner(_states()), feed=None)  # type: ignore[arg-type]
    c = TestClient(create_app(app_state))
    assert c.get("/api/v2/clock").status_code == 200
    assert c.get("/api/v2/toplists/rvol").status_code == 200


class _Resp:
    def __init__(self, status: int, body: dict) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> dict:
        return self._body


# ── helpers ──────────────────────────────────────────────────────────────────

def test_symbolstate_still_has_last_close_attribute():
    """price_of() is the only reader of the private attribute; fail loudly if the engine renames it."""
    st = _state()
    assert hasattr(st, "_last_close")
    _feed(st, [10.0, 11.0], start="09:30")
    assert price_of(st) == 11.0


def test_clean_handles_nan_numpy_and_timestamps():
    import numpy as np
    out = clean({"a": float("nan"), "b": np.float64(1.5), "c": pd.Timestamp("2026-01-01", tz="UTC"), "d": [np.int64(3)]})
    assert out["a"] is None and out["b"] == 1.5 and out["c"].startswith("2026-01-01") and out["d"] == [3]


def test_session_now_phases():
    et = ZoneInfo("America/New_York")
    assert session_now(datetime(2026, 9, 3, 8, 0, tzinfo=et))[0] == "pre"       # Thu
    assert session_now(datetime(2026, 9, 3, 10, 0, tzinfo=et))[0] == "rth"
    assert session_now(datetime(2026, 9, 3, 17, 0, tzinfo=et))[0] == "post"
    assert session_now(datetime(2026, 9, 3, 22, 0, tzinfo=et))[0] == "closed"
    s, nxt = session_now(datetime(2026, 9, 5, 12, 0, tzinfo=et))                # Sat
    assert s == "closed" and nxt.weekday() == 0


# ── json_store ───────────────────────────────────────────────────────────────

def test_layout_store_roundtrip_and_atomic(tmp_path: Path):
    ls = LayoutStore(tmp_path / "layouts")
    ls.save({"id": "main", "name": "Main", "version": 1, "layout": [], "windows": {}})
    ls.save({"id": "main", "name": "Main 2", "version": 1, "layout": [], "windows": {}})
    docs = ls.load_all()
    assert [d["name"] for d in docs] == ["Main 2"]
    assert not list((tmp_path / "layouts").glob("*.tmp"))
    (tmp_path / "layouts" / "bad.json").write_text("{not json", encoding="utf-8")
    assert [d["id"] for d in ls.load_all()] == ["main"]
    with pytest.raises(ValueError):
        ls.save({"id": "../evil"})
    assert ls.delete("main") is True and ls.delete("main") is False


def test_watchlist_store_upsert_and_normalize(tmp_path: Path):
    ws = WatchlistStore(tmp_path / "wl.json")
    ws.save({"id": "w1", "name": "A", "symbols": [" aapl", "MSFT", "aapl", ""]})
    assert ws.load_all()[0]["symbols"] == ["AAPL", "MSFT"]
    ws.save({"id": "w1", "name": "B", "symbols": ["x"]})
    assert [w["name"] for w in ws.load_all()] == ["B"]
    assert ws.delete("w1") and ws.load_all() == []
    assert normalize_symbols(["a", 1, "A"]) == ["A"]


def test_watchlist_metadata_and_universe_selection_roundtrip(tmp_path: Path):
    ws = WatchlistStore(tmp_path / "wl.json")
    rec = ws.save({"id": "movers", "name": "Morning movers", "description": "Liquid names only",
                   "symbols": ["AAPL"], "source": "yahoo_screener", "sourceLabel": "Day gainers",
                   "capturedAt": "2026-09-21T10:00:00Z"})
    assert rec["description"] == "Liquid names only"
    assert rec["source"] == "yahoo_screener" and rec["sourceLabel"] == "Day gainers"
    selection = UniverseSelectionStore(tmp_path / "selection.json")
    assert selection.load() is None
    assert selection.save("movers") == "movers" and selection.load() == "movers"
    assert selection.save(None) is None and selection.load() is None


# ── events ───────────────────────────────────────────────────────────────────

def test_event_buffer_paging():
    b = EventBuffer(maxlen=5)
    for i in range(7):
        b.push("HOD", f"S{i}", float(i), "2026-09-04T10:00:00-04:00")
    latest, evs = b.since(0, limit=100)
    assert latest == 7 and [e["seq"] for e in evs] == [3, 4, 5, 6, 7]
    latest, evs = b.since(6)
    assert [e["seq"] for e in evs] == [7]
    assert b.since(7)[1] == []


def test_hodlod_hook_seeds_then_emits():
    st = _state(symbol="AAA")
    scanner = FakeScanner({"AAA": st})
    buf = EventBuffer()
    hook = make_hodlod_hook(scanner, buf)
    ts0 = pd.Timestamp("2026-09-04 09:30", tz="America/New_York")
    def bar(i: int, px: float) -> dict:
        return {"symbol": "AAA", "timestamp": ts0 + pd.Timedelta(minutes=i), "open": px, "high": px, "low": px, "close": px, "volume": 1000.0}
    st.on_bar(bar(0, 100.0)); hook(bar(0, 100.0))
    assert buf.seq == 0                                  # first observation seeds silently
    st.on_bar(bar(1, 101.0)); hook(bar(1, 101.0))
    st.on_bar(bar(2, 99.0)); hook(bar(2, 99.0))
    st.on_bar(bar(3, 100.5)); hook(bar(3, 100.5))        # inside range: nothing
    _, evs = buf.since(0)
    assert [(e["type"], e["price"]) for e in evs] == [("HOD", 101.0), ("LOD", 99.0)]
    hook({"symbol": "SPY", "timestamp": ts0})           # ignored, never raises
    hook({"symbol": "ZZZ", "timestamp": ts0})


# ── toplists ─────────────────────────────────────────────────────────────────

def test_build_rows_orderings():
    states = list(_states().items())
    g = build_rows(states, "gainers_close", 10)
    assert g[0]["symbol"] == "AAA" and g[-1]["symbol"] == "BBB"
    lo = build_rows(states, "losers_close", 10)
    assert lo[0]["symbol"] == "BBB"
    mv = build_rows(states, "movers_5m", 10)
    assert mv[0]["symbol"] in ("AAA", "BBB") and abs(mv[0]["value"]) >= abs(mv[-1]["value"])
    assert build_rows(states, "rvol", 1) == [] or len(build_rows(states, "rvol", 1)) == 1
    with pytest.raises(ValueError):
        build_rows(states, "nope", 5)


def test_toplist_engine_caches(monkeypatch):
    scanner = FakeScanner(_states())
    eng = ToplistEngine(scanner, ttl=60)
    a = eng.compute("gainers_close", 5)
    scanner._states = {}                                  # would compute empty if not cached
    b = eng.compute("gainers_close", 5)
    assert a["rows"] == b["rows"] and a["list"] == "gainers_close"


# ── news ─────────────────────────────────────────────────────────────────────

def test_news_client_parses_and_caches_and_fails_open():
    calls = []
    def get_fn(url, headers, params, timeout):
        calls.append(params)
        return _Resp(200, {"news": [{"id": 9, "headline": "H <i>x</i>", "summary": "s", "url": "u", "source": "src",
                                     "symbols": ["A"], "images": [{"size": "large", "url": "L"}], "created_at": "t"}]})
    nc = NewsClient("k", "s", ttl=60, get_fn=get_fn)
    r1 = nc.fetch(["a", "A"], limit=10, hours=6)
    assert r1["items"][0]["headline"] == "H x" and r1["items"][0]["image"] == "L" and r1["stale"] is False
    assert calls[0]["symbols"] == "A" and calls[0]["limit"] == 10 and "start" in calls[0]
    nc.fetch(["A"], limit=10, hours=6)
    assert len(calls) == 1                                # cached
    bad = NewsClient("k", "s", get_fn=lambda *a, **k: _Resp(429, {}))
    r = bad.fetch(None)
    assert r["stale"] is True and r["items"] == [] and "429" in r["error"]


def test_news_client_merges_rss_and_widens_an_empty_window():
    from datetime import datetime, timedelta, timezone
    from email.utils import format_datetime
    now = datetime.now(timezone.utc)

    def rss(*entries):
        body = "".join(f"<item><title>{t}</title><link>http://x/{i}</link><guid>{i}</guid>"
                       f"<pubDate>{format_datetime(now - timedelta(hours=h))}</pubDate></item>"
                       for i, (t, h) in enumerate(entries))
        r = _Resp(200, {})
        r.text = f"<rss><channel>{body}</channel></rss>"
        return r

    wire = lambda url, headers, params, timeout: _Resp(200, {"news": [
        {"id": 1, "headline": "Same story", "url": "w", "source": "benzinga", "symbols": ["A"],
         "created_at": (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")}]})
    nc = NewsClient("k", "s", get_fn=wire, rss_sources=("yahoo",),
                    rss_get_fn=lambda url, headers, params, timeout: rss(("Same story!", 2), ("Fresh", 0.5), ("Old", 90)))
    r = nc.fetch(["A"], limit=10, hours=24)
    assert [i["headline"] for i in r["items"]] == ["Fresh", "Same story"]      # newest first, deduped, "Old" cut
    assert r["items"][0]["source"] == "Yahoo Finance" and "widened_hours" not in r

    # no keys + nothing inside the window: falls back to the latest it can find
    quiet = NewsClient(None, None, rss_sources=("yahoo",), rss_get_fn=lambda *a, **k: rss(("Old", 90)))
    r = quiet.fetch(["A"], limit=10, hours=24)
    assert [i["headline"] for i in r["items"]] == ["Old"] and r["widened_hours"] == 720 and r["stale"] is False

    # one dead feed does not take the others down
    def flaky(url, headers, params, timeout):
        return _Resp(503, {}) if "nasdaq" in url else rss(("Up", 1))
    r = NewsClient(None, None, rss_sources=("yahoo", "nasdaq"), rss_get_fn=flaky).fetch(["A"])
    assert [i["headline"] for i in r["items"]] == ["Up"] and r["stale"] is False


# ── fundamentals ─────────────────────────────────────────────────────────────

class _FakeTicker:
    def __init__(self, sym: str) -> None:
        self.info = {"longName": f"{sym} Inc", "sector": "Tech", "industry": "Chips", "marketCap": 1e9,
                     "sharesOutstanding": 1e6, "floatShares": 9e5, "shortPercentOfFloat": 0.02, "shortRatio": 1.5}
    def get_earnings_dates(self, limit=8):
        idx = pd.DatetimeIndex([pd.Timestamp("2020-01-01", tz="America/New_York"), pd.Timestamp("2099-01-15", tz="America/New_York")])
        return pd.DataFrame({"EPS Estimate": [1.0, 1.0]}, index=idx)


def test_fundamentals_cache_fetch_and_persist(tmp_path: Path):
    fc = FundamentalsCache(tmp_path / "f.json", ticker_factory=_FakeTicker)
    assert fc.get("AAA") is None
    e = fc.fetch_one("aaa")
    assert e["ok"] and e["name"] == "AAA Inc" and e["next_earnings"] == "2099-01-15" and e["float_shares"] == 9e5
    fc.flush()
    doc = json.loads((tmp_path / "f.json").read_text(encoding="utf-8"))
    assert "AAA" in doc["symbols"]
    fc2 = FundamentalsCache(tmp_path / "f.json", ticker_factory=_FakeTicker)
    assert fc2.get("AAA")["ok"] is True
    def boom(sym): raise RuntimeError("no network")
    fc3 = FundamentalsCache(tmp_path / "g.json", ticker_factory=boom)
    assert fc3.fetch_one("ZZZ")["ok"] is False and fc3.get("ZZZ") is not None


# ── routes ───────────────────────────────────────────────────────────────────

def test_v2_clock_and_state_and_snapshot(client: TestClient):
    c = client.get("/api/v2/clock").json()
    assert c["session"] in ("pre", "rth", "post", "closed") and c["regime"] == "bullish" and c["spy"]["price"] == 452.0
    s = client.get("/api/v2/state/aaa").json()
    assert s["found"] and s["symbol"] == "AAA" and s["price"] == 106.0 and s["sector_etf"] == "XLK"
    assert client.get("/api/v2/state/NOPE").status_code == 404
    snap = client.get("/api/v2/snapshot?symbols=AAA,BBB,NOPE").json()
    assert set(snap["rows"]) == {"AAA", "BBB"} and snap["rows"]["BBB"]["price"] == 94.0


def test_v2_toplists_events_news_meta(client: TestClient):
    t = client.get("/api/v2/toplists/gainers_close?limit=2").json()
    assert t["list"] == "gainers_close" and [r["symbol"] for r in t["rows"]] == ["AAA", "CCC"]
    assert client.get("/api/v2/toplists/bogus").status_code == 404
    ev = client.get("/api/v2/events?since=0").json()
    assert ev["seq"] == 0 and ev["events"] == []
    # The stream now reports which universe filter scoped it (none here).
    assert ev["universe"]["id"] is None and ev["universe"]["scope"] == "static"
    n = client.get("/api/v2/news?symbols=AAA&limit=5&hours=6").json()
    assert n["items"][0]["headline"] == "Hello & world" and n["items"][0]["image"] == "http://img/t"
    m = client.get("/api/v2/universe/meta").json()
    assert m["symbols"] == ["AAA", "BBB", "CCC"] and m["meta"]["AAA"]["sector_etf"] == "XLK"


def test_v2_fundamentals_pending(client: TestClient):
    r = client.get("/api/v2/fundamentals/AAA").json()
    assert r["symbol"] == "AAA" and r["ok"] is False and r["pending"] is True


def test_v2_layouts_crud_and_validation(client: TestClient):
    screen = {"id": "s1", "name": "One", "version": 1, "createdAt": "x", "updatedAt": "x", "locked": False,
              "layout": [{"i": "w1", "x": 0, "y": 0, "w": 6, "h": 10}], "windows": {"w1": {"id": "w1", "type": "clock"}}}
    assert client.put("/api/v2/layouts/s1", json=screen).json() == {"ok": True, "id": "s1"}
    assert [s["id"] for s in client.get("/api/v2/layouts").json()["screens"]] == ["s1"]
    assert client.put("/api/v2/layouts/other", json=screen).status_code == 400
    bad = {**screen, "windows": {}}
    assert client.put("/api/v2/layouts/s1", json=bad).status_code == 400
    assert client.put("/api/v2/layouts/s1", json={**screen, "version": 2}).status_code == 400
    assert client.delete("/api/v2/layouts/s1").json()["ok"] is True
    assert client.get("/api/v2/layouts").json()["screens"] == []


def test_v2_watchlists_crud(client: TestClient):
    r = client.put("/api/v2/watchlists/w1", json={"id": "w1", "name": "Core", "symbols": ["aapl", "AAPL", "msft"]}).json()
    assert r["ok"] and r["watchlist"]["symbols"] == ["AAPL", "MSFT"]
    assert client.get("/api/v2/watchlists").json()["watchlists"][0]["name"] == "Core"
    assert client.put("/api/v2/watchlists/w2", json={"id": "w1"}).status_code == 400
    assert client.delete("/api/v2/watchlists/w1").json()["ok"] is True


def test_v2_screener_and_explicit_universe_selection(client: TestClient):
    assert any(p["id"] == "day_gainers" for p in client.get("/api/v2/screener/yahoo/catalog").json()["presets"])
    screened = client.post("/api/v2/screener/yahoo", json={"mode": "preset", "preset": "day_gainers", "limit": 25}).json()
    assert screened["rows"][0]["symbol"] == "AAPL"

    symbols = [f"S{i}" for i in range(288)]
    assert client.put("/api/v2/watchlists/core", json={"id": "core", "name": "Core", "symbols": symbols}).status_code == 200
    selected = client.put("/api/v2/universe/selection", json={"watchlist_id": "core"}).json()
    assert selected["ok"] and selected["watchlist_id"] == "core" and selected["applies_on_restart"]
    too_many = [f"X{i}" for i in range(289)]
    assert client.put("/api/v2/watchlists/core", json={"id": "core", "name": "Core", "symbols": too_many}).status_code == 400
    assert client.delete("/api/v2/watchlists/core").json()["ok"]
    assert client.get("/api/v2/universe/selection").json()["watchlist_id"] is None


def test_v2_static_mount_without_build(client: TestClient):
    r = client.get("/v2/anything")
    assert r.status_code in (200, 503)   # 503 until dashboard-v2/dist exists; 200 once built


def test_v2_static_serves_javascript_as_module(tmp_path):
    from fastapi import FastAPI
    from scanner.api_v2 import mount_v2_static

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html></html>", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("export {};", encoding="utf-8")
    app = FastAPI()
    mount_v2_static(app, dist)

    response = TestClient(app).get("/v2/assets/app.js")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/javascript")
