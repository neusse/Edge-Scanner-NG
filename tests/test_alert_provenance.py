"""Detector revisions and evaluated-bar provenance survive edits and recovery."""
from datetime import date
import json

import pandas as pd

from scanner.alert_provenance import custom_revision, source_bar
from scanner.alert_store import AlertStore
from scanner.custom_setups import CustomEvaluator, CustomSetupStore
from scanner.feed_hub import FeedHub, Subscription
from scanner.live_scanner import LiveScanner
from tests.helpers import _bar, _state


def test_custom_revision_pins_saved_document_across_edit(tmp_path):
    store = CustomSetupStore(tmp_path / "setups", defaults=tmp_path / "none.json")
    first = store.save({"id": "my_hod", "name": "Original", "triggers": [
        {"id": "hod", "options": ["high"]}]})
    ev = CustomEvaluator(store)
    st = _state()
    a = None
    for minute, price in ((30, 100), (31, 101)):
        bar = _bar(price, et=f"2026-09-23 09:{minute:02d}", sym=st.symbol)
        st.on_bar(bar)
        alerts = ev.on_bar(st, bar)
        if alerts:
            a = alerts[0]
    assert a is not None
    assert a["detector_setup_revision"] == custom_revision(first)

    second = store.save({**first, "name": "Edited"}, sid="my_hod")
    ev.reload()
    bar = _bar(102, et="2026-09-23 09:32", sym=st.symbol)
    st.on_bar(bar)
    b = ev.on_bar(st, bar)[0]
    assert b["detector_setup_revision"] == custom_revision(second)
    assert a["detector_setup_revision"] != b["detector_setup_revision"]
    assert a["setup_label"] == "Original" and b["setup_label"] == "Edited"


def test_system_and_custom_share_bar_identity_and_effective_revision():
    scanner = LiveScanner([], None)
    state = _state()
    bar = _bar(101, et="2026-09-23 09:31", sym=state.symbol)
    bar["source"] = "schwab_chart_equity"
    system = {"setup": "HOD_BREAKOUT", "symbol": state.symbol, "config_hash": "default"}
    custom = {"setup": "my_hod", "symbol": state.symbol,
              "detector_setup_revision": "custom-sha256:saved-snapshot"}
    for alert, kind in ((system, "system"), (custom, "custom")):
        assert scanner._passes_profile(alert, state, bar, "rth", None, kind)
        assert alert["detector_revision"].startswith("detector-sha256:")
        assert alert["source_bar"]["complete"] is True
        assert alert["source_bar"]["source"] == "schwab_chart_equity"
    assert system["source_bar"]["id"] == custom["source_bar"]["id"]
    assert system["detector_setup_revision"].startswith("system-sha256:")
    assert system["detector_revision"] != custom["detector_revision"]


def test_quote_bar_is_distinct_and_unknown_is_not_promoted_to_complete():
    bar = {"symbol": "AAPL", "timestamp": pd.Timestamp("2026-09-23 14:31", tz="UTC")}
    chart = source_bar({**bar, "source": "schwab_chart_equity"})
    quotes = source_bar({**bar, "source": "quotes"})
    unknown = source_bar(bar)
    assert chart["id"] != quotes["id"]
    assert quotes["source"] == "schwab_quote_derived" and quotes["complete"] is True
    assert unknown["complete"] is None


def test_recovery_preserves_revision_and_old_archive_is_unknown(tmp_path):
    hub = FeedHub(store_dir=tmp_path)
    bar = source_bar({"symbol": "AAPL", "timestamp": pd.Timestamp("2026-09-23 14:31", tz="UTC"),
                      "source": "schwab_chart_equity"})
    sent = hub.publish({"setup": "my_hod", "symbol": "AAPL", "detector_setup_revision": "custom-sha256:old",
                        "detector_revision": "detector-sha256:old", "source_bar": bar}, "custom")
    recovered = hub.recover_for(Subscription(), None)["alerts"][0]
    assert recovered["event_id"] == sent["event_id"]
    assert recovered["detector_revision"] == "detector-sha256:old"
    assert recovered["source_bar"] == bar
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / f"{date.today().isoformat()}.jsonl").write_text(
        json.dumps({"symbol": "AAPL", "setup": "my_hod"}) + "\n", encoding="utf-8")
    old = AlertStore(legacy_dir).load_recent()[0]
    assert old["detector_revision"] is None and old["source_bar"] is None


def test_replay_publication_preserves_provenance(tmp_path):
    hub = FeedHub(store_dir=tmp_path, mode="replay")
    a = hub.publish({"setup": "x", "detector_revision": "detector-sha256:replay",
                     "source_bar": source_bar({"symbol": "AAPL", "timestamp": pd.Timestamp("2026-08-20 14:31", tz="UTC"),
                                               "source": "alpaca_minute_bar"})}, "custom")
    assert a["mode"] == "replay"
    assert hub.recover_for(Subscription(), None)["alerts"][0]["source_bar"]["source"] == "alpaca_minute_bar"
