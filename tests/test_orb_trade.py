"""Public ORB timing boundary: completed bars versus fresh trade observations."""
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scanner.api import AppState
from scanner.api_v2 import register_v2_routes
from scanner.alert_sink import AlertSink
from scanner.custom_setups import CustomEvaluator, CustomSetupStore, SetupError
from scanner.data.schwab import SchwabFeed
from scanner.feed_hub import FeedHub, Subscription
from scanner.live_scanner import LiveScanner
from scanner.orb_trade import OpeningRangeBook, OrbTradeCross
from scanner.quote_state import QuoteBook
from tests.test_live_scanner import _FakeFeed, _bar, _warmup
from tests.test_alert_feed_contract import VALIDATOR


DAY = "2026-09-23"


def _ms(clock: str) -> int:
    return int(pd.Timestamp(f"{DAY} {clock}", tz="America/New_York").timestamp() * 1000)


def _trade(clock: str, price: float, *, generation=0, lag=300, **updates) -> dict:
    stamp = _ms(clock)
    event = {"symbol": "AAPL", "price": price, "trade_market_ms": stamp,
             "receipt_ms": stamp + lag, "stream_id": "test-stream",
             "connection_epoch": generation, "source": "schwab_levelone",
             "tier": "stream", "coverage": "live", "quality": "valid",
             "delayed": False, "stream_active": True}
    event.update(updates)
    return event


def _opening(interval=5, source="schwab_chart_equity"):
    book = OpeningRangeBook()
    for offset in range(interval):
        bar = _bar("AAPL", 100.0, f"{DAY} 09:{30 + offset:02d}")
        bar["high"] = 100.5
        bar["source"] = source
        book.on_bar(bar)
    return book, book.completed("AAPL", DAY, interval)


def test_range_requires_every_authoritative_completed_minute():
    book, opening = _opening()
    assert opening["complete"] is True and opening["high"] == 100.5
    assert opening["end"] == pd.Timestamp(f"{DAY} 09:35", tz="America/New_York").tz_convert("UTC").isoformat()
    assert book.completed("AAPL", DAY, 15) is None
    assert book.completed("AAPL", "2026-09-24", 5) is None
    _, synthetic = _opening(source="quotes")
    assert synthetic is None


def test_first_trade_above_is_not_a_cross_and_same_day_never_rearms():
    _, opening = _opening()
    tracker = OrbTradeCross()
    assert tracker.observe(_trade("09:35:01", 101), opening, 5) is None
    assert tracker.observe(_trade("09:35:02", 100.5), opening, 5) is None
    fired = tracker.observe(_trade("09:35:03", 100.51), opening, 5)
    assert fired["trade_price"] == 100.51
    assert fired["trade_market_timestamp"] == pd.Timestamp(
        f"{DAY} 09:35:03", tz="America/New_York").tz_convert("UTC").isoformat()
    assert fired["latency_ms"] == 300
    assert tracker.observe(_trade("09:35:04", 100.4), opening, 5) is None
    assert tracker.observe(_trade("09:35:05", 101), opening, 5) is None


def test_missing_stale_duplicate_out_of_order_and_reconnect_fail_closed():
    _, opening = _opening()
    tracker = OrbTradeCross()
    assert tracker.observe(_trade("09:35:00", 100), opening, 5) is None
    assert tracker.observe(_trade("09:35:00", 101), opening, 5) is None  # duplicate time
    assert tracker.observe(_trade("09:34:59", 101), opening, 5) is None  # before completion
    assert tracker.observe(_trade("09:35:01", 101, lag=3000), opening, 5) is None
    assert tracker.observe(_trade("09:35:02", 101), opening, 5) is None  # no baseline after stale
    assert tracker.observe(_trade("09:35:03", 100), opening, 5) is None
    assert tracker.observe(_trade("09:35:04", 101, generation=1), opening, 5) is None
    assert tracker.observe(_trade("09:35:05", 100, generation=1), opening, 5) is None
    assert tracker.observe(_trade("09:35:20", 101, generation=1), opening, 5) is None  # gap > 10s
    assert tracker.observe(_trade("09:35:21", 100, generation=1), opening, 5) is None
    assert tracker.observe(_trade("09:35:22", 101, generation=1,
                                  coverage="reconnecting"), opening, 5) is None
    assert tracker.observe(_trade("09:35:23", 101, generation=1), opening, 5) is None
    assert tracker.observe(_trade("09:35:24", 100, generation=1), opening, 5) is None
    assert tracker.observe(_trade("09:35:25", 101, generation=1,
                                  delayed=True), opening, 5) is None
    assert tracker.observe(_trade("09:35:26", 101, generation=1), opening, 5) is None


def test_fifteen_minute_range_requires_0945_and_premarket_never_arms():
    _, opening = _opening(15)
    tracker = OrbTradeCross()
    assert tracker.observe(_trade("09:29:59", 100), opening, 15) is None
    assert tracker.observe(_trade("09:44:59", 100), opening, 15) is None
    assert tracker.observe(_trade("09:45:00", 100), opening, 15) is None
    assert tracker.observe(_trade("09:45:01", 100.51), opening, 15)["opening_range"]["interval_min"] == 15


def test_bar_close_orb_ignores_wick_that_rejects_high(tmp_path):
    feed = _FakeFeed()
    scanner = LiveScanner(["AAPL"], feed)
    _warmup(scanner)
    store = CustomSetupStore(tmp_path / "setups", defaults=tmp_path / "none.json")
    store.save({"id": "orb_close", "name": "Close ORB", "triggers": [
        {"id": "orb_breakout", "options": ["5"]}]})
    sink = AlertSink()
    scanner.attach_custom(CustomEvaluator(store), sink)
    for minute in range(30, 36):
        bar = _bar("AAPL", 100, f"{DAY} 09:{minute:02d}")
        bar["source"] = "schwab_chart_equity"
        if minute == 35:
            bar["high"] = 101  # wick through 100.05, close back at 100
        scanner._on_bar(bar)
    assert sink.all() == []
    scanner._on_bar({**_bar("AAPL", 100.2, f"{DAY} 09:36"), "source": "schwab_chart_equity"})
    assert sink.all()[0]["eventSemantics"] == "bar-close-cross"
    assert sink.all()[0]["trigger_evidence"][0]["eventSemantics"] == "bar-close-cross"


def test_raw_levelone_pair_required_and_sparse_cached_last_cannot_fire():
    book = QuoteBook()
    observed = []
    baseline = {"data": [{"service": "LEVELONE_EQUITIES", "content": [{
        "key": "AAPL", "delayed": False, "1": 100, "2": 100.02,
        "37": _ms("09:35:00"), "38": _ms("09:35:00"),
        "3": 100, "35": _ms("09:35:00")}]}]}
    SchwabFeed.handle_quotes(baseline, None, book, observed.append)
    assert len(observed) == 1
    stale_last = {"data": [{"service": "LEVELONE_EQUITIES", "content": [{
        "key": "AAPL", "1": 101, "37": _ms("09:35:01")}]}]}
    SchwabFeed.handle_quotes(stale_last, None, book, observed.append)
    assert len(observed) == 1
    SchwabFeed.handle_quotes({"data": [{"service": "LEVELONE_EQUITIES", "content": [{
        "key": "AAPL", "3": 101}]}]}, None, book, observed.append)
    assert len(observed) == 1  # price without own trade time
    SchwabFeed.handle_quotes({"data": [{"service": "LEVELONE_EQUITIES", "content": [{
        "key": "AAPL", "35": _ms("09:35:02")}]}]}, None, book, observed.append)
    assert len(observed) == 1  # market time without fresh price


def test_trade_alert_precedes_next_completed_bar_and_survives_archive(tmp_path):
    feed = _FakeFeed()
    scanner = LiveScanner(["AAPL"], feed)
    _warmup(scanner)
    store = CustomSetupStore(tmp_path / "setups", defaults=tmp_path / "none.json")
    store.save({"id": "orb_fast", "name": "Fast ORB", "mode": "or",
                "triggers": [{"id": "orb_trade_cross", "options": ["5"]}]})
    sink = AlertSink()
    scanner.attach_custom(CustomEvaluator(store), sink)
    for minute in range(30, 35):
        bar = _bar("AAPL", 100, f"{DAY} 09:{minute:02d}")
        bar["source"] = "schwab_chart_equity"
        scanner._on_bar(bar)
    assert sink.all() == []
    scanner._on_trade(_trade("09:35:01", 100))
    assert sink.all() == []
    scanner._on_trade(_trade("09:35:02", 100.06))
    assert len(sink.all()) == 1
    alert = sink.all()[0]
    assert alert["eventSemantics"] == "trade-cross"
    assert alert["trade_cross"]["trade_price"] == 100.06
    assert alert["trade_cross"]["opening_range"]["interval_min"] == 5
    assert alert["source_bar"]["market_timestamp"] == pd.Timestamp(
        f"{DAY} 09:34", tz="America/New_York").tz_convert("UTC").isoformat()
    assert alert["detector_setup_revision"].startswith("custom-sha256:")
    assert alert["detector_revision"].startswith("detector-sha256:")
    hub = FeedHub(store_dir=tmp_path / "alerts", mode="replay")
    sent = hub.publish(alert, "custom")
    VALIDATOR.validate({"type": "alert", "schema_version": 1,
                        **{key: sent[key] for key in (
                            "session_id", "mode", "emitted_at", "event_id", "seq", "market_timestamp")},
                        "alert": sent})
    recovered = hub.recover_for(Subscription(), None)["alerts"][0]
    assert recovered["eventSemantics"] == "trade-cross"
    assert recovered["trade_cross"] == sent["trade_cross"]
    assert recovered["detector_revision"] == sent["detector_revision"]


def test_no_trade_trigger_on_bar_and_mode_revision_changes(tmp_path):
    store = CustomSetupStore(tmp_path / "setups", defaults=tmp_path / "none.json")
    first = store.save({"id": "orb", "name": "ORB", "triggers": [
        {"id": "orb_breakout", "options": ["5"]}]})
    from scanner.alert_provenance import custom_revision
    second = store.save({**first, "triggers": [{"id": "orb_trade_cross", "options": ["5"]}]}, sid="orb")
    assert custom_revision(first) != custom_revision(second)
    with pytest.raises(SetupError, match="OR mode"):
        store.save({"id": "bad", "name": "bad", "mode": "and", "triggers": [
            {"id": "orb_trade_cross", "options": ["5"]}]})
    feed = _FakeFeed()
    scanner = LiveScanner(["AAPL"], feed)
    _warmup(scanner)
    sink = AlertSink()
    scanner.attach_custom(CustomEvaluator(store), sink)
    for minute in range(30, 37):
        bar = _bar("AAPL", 100 if minute < 35 else 101, f"{DAY} 09:{minute:02d}")
        bar["source"] = "schwab_chart_equity"
        scanner._on_bar(bar)
    assert sink.all() == []  # completed-bar crossing cannot masquerade as trade-cross


def test_catalog_semantics_capability_and_revision_follow_selected_mode(tmp_path):
    feed = _FakeFeed()
    scanner = LiveScanner(["AAPL"], feed)
    _warmup(scanner)
    app_state = AppState(scanner=scanner, feed=feed)
    store = CustomSetupStore(tmp_path / "setups" / "custom", defaults=tmp_path / "none.json")
    evaluator = CustomEvaluator(store)
    scanner.attach_custom(evaluator, AlertSink())
    app_state.custom_eval = evaluator
    app = FastAPI()
    register_v2_routes(app, app_state, setups_dir=tmp_path / "setups")
    client = TestClient(app)
    old = client.put("/api/v2/setups/orb", json={"id": "orb", "name": "ORB",
        "triggers": [{"id": "orb_breakout", "options": ["5"]}]}).json()["setup"]
    catalog = client.get("/api/v2/setups").json()
    assert catalog["custom"][0]["eventSemantics"] == "bar-close-cross"
    assert catalog["custom"][0]["triggers"][0]["eventSemantics"] == "bar-close-cross"
    assert next(t for t in catalog["catalog"] if t["id"] == "orb_trade_cross")["eventSemantics"] == "trade-cross"
    client.put("/api/v2/setups/orb", json={"id": "orb", "name": "ORB",
        "triggers": [{"id": "orb_trade_cross", "options": ["5"]}]})
    trade = client.get("/api/v2/setups").json()["custom"][0]
    assert trade["eventSemantics"] == "trade-cross"
    assert trade["tradeCrossCapability"] == "unavailable"  # fake feed cannot prove raw trades
    assert trade["detector_setup_revision"] != old["detector_setup_revision"]
    assert client.get("/api/v2/setups/orb/check?symbol=AAPL").json()["tradeCrossStatus"]["5"] == {
        "state": "unavailable", "reason": "feed_has_no_trade_updates"}
    feed.supports_trade_updates = True
    assert client.get("/api/v2/setups").json()["custom"][0]["tradeCrossCapability"] == "configured"


def test_archive_restoration_keeps_same_day_trade_cross_latched():
    _, opening = _opening()
    previous = {"symbol": "AAPL", "eventSemantics": "trade-cross", "trade_cross": {
        "trade_market_timestamp": pd.Timestamp(f"{DAY} 09:35", tz="America/New_York").tz_convert("UTC").isoformat(),
        "opening_range": opening}}
    tracker = OrbTradeCross()
    tracker.restore([previous])
    assert tracker.observe(_trade("09:35:10", 100), opening, 5) is None
    assert tracker.observe(_trade("09:35:11", 101), opening, 5) is None
