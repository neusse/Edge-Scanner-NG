from __future__ import annotations

import time
from datetime import datetime, timezone

import pandas as pd
import pytest

from scanner.dynamic_universe import ManualPromotionController, PromotionError


def _frame(times: list[str], closes: list[float]) -> pd.DataFrame:
    idx = pd.to_datetime(times, utc=True)
    return pd.DataFrame({
        "open": closes, "high": [v + .1 for v in closes],
        "low": [v - .1 for v in closes], "close": closes,
        "volume": [100.0] * len(closes),
    }, index=idx)


class _Scanner:
    def __init__(self) -> None:
        self.added = None
        self.primed = []
        self.listener = None
        self.releases = []

    def set_dynamic_readiness_listener(self, listener):
        self.listener = listener

    def add_dynamic_symbol(self, symbol, daily, bars_5m, session_bars, *, evaluation_enabled,
                           dynamic_epoch=None):
        self.added = (symbol, daily, bars_5m, session_bars, evaluation_enabled, dynamic_epoch)

    def activate_dynamic_symbol(self, symbol, epoch):
        return {
            "ready": True,
            "components": {"daily": {"available": True}, "session": {"available": True}},
            "setups": [{"id": "hod", "name": "New HOD", "status": "ready", "reasons": []}],
            "ready_setup_ids": ["hod"],
        }

    def prime_stream_bar(self, bar):
        self.primed.append(bar)

    def begin_dynamic_release(self, symbol, epoch):
        self.releases.append(("begin", symbol, epoch))

    def finish_dynamic_release(self, symbol, epoch):
        self.releases.append(("finish", symbol, epoch))

    def reconnect_dynamic_symbol(self, symbol, old_epoch, new_epoch):
        self.releases.append(("reconnect", symbol, old_epoch, new_epoch))
        return self.added is not None


class _Feed:
    def __init__(self) -> None:
        self.controller = None
        self.requested = []
        self.stream = object()
        self.daily = _frame(["2026-09-23", "2026-09-24"], [9.0, 10.0])
        self.profile = _frame(["2026-09-24T13:30:00Z"], [10.0])
        self.session = _frame(
            ["2026-09-25T13:30:00Z", "2026-09-25T13:31:00Z"], [10.0, 10.1]
        )
        self.stats = {"daily": {"current": 1, "requests": 0},
                      "5m": {"current": 1, "requests": 0}}
        self.external_watches = set()
        self.released = []

    def set_dynamic_controller(self, controller): self.controller = controller
    def dynamic_capacity(self):
        return {"chart": {"used": 294, "cap": 300, "headroom": 5, "available": 1},
                "level_one": {"used": 294, "cap": 3000, "headroom": 0, "available": 2706}}
    def request_dynamic_membership(self, symbol): self.requested.append(symbol)
    def request_dynamic_release(self, symbol): self.released.append(symbol)
    def is_external_quote_watch(self, symbol): return symbol in self.external_watches
    def history_stats(self): return self.stats
    def get_historical_daily(self, *args): return self.daily
    def get_historical_bars(self, *args): return self.profile
    def get_todays_bars(self, *args): return self.session

    def acknowledge(self, symbol):
        self.controller.on_membership(symbol, {
            "chart": "acknowledged", "level_one": "acknowledged", "error": None,
        })


def test_manual_promotion_waits_for_both_acknowledgements_then_enables_ready_setups():
    scanner, feed = _Scanner(), _Feed()
    controller = ManualPromotionController(scanner, feed, clock=lambda: 1_798_000_000.0)
    requested = controller.admit("NEW", {"price": 10.0})
    assert requested["state"] == "requested" and feed.requested == ["NEW"]
    assert scanner.added is None and feed.stream is feed.stream

    stream_bar = {"symbol": "NEW", "timestamp": pd.Timestamp("2026-09-25T13:31:00Z"),
                  "source": "schwab_chart_equity", "open": 10.1, "high": 10.3,
                  "low": 10.0, "close": 10.2, "volume": 120.0}
    assert controller.on_bar(stream_bar) is True
    feed.acknowledge("NEW")
    deadline = time.time() + 2
    while controller.status("NEW")["state"] not in {"ready", "failed"} and time.time() < deadline:
        time.sleep(.01)
    ready = controller.status("NEW")
    assert ready["state"] == "ready"
    assert ready["setup_evaluation"] == "enabled"
    assert ready["readiness"]["ready_setup_ids"] == ["hod"]
    assert scanner.added[0] == "NEW" and scanner.added[4] is False
    assert scanner.added[5] == requested["epoch"]
    assert scanner.added[3][-1]["close"] == 10.2  # current stream wins overlap
    assert ready["merge"]["deduplicated"] == 1
    assert ready["merge"]["material_disagreements"] == 1


def test_manual_promotion_rejects_bad_candidate_and_honors_failed_cooldown():
    scanner, feed = _Scanner(), _Feed()
    controller = ManualPromotionController(scanner, feed, clock=lambda: 100.0)
    with pytest.raises(PromotionError, match="current discovery"):
        controller.admit("NEW", None)
    with pytest.raises(PromotionError, match="positive price"):
        controller.admit("NEW", {"price": 0})
    controller.admit("NEW", {"price": 10})
    controller.on_membership("NEW", {"chart": "rejected", "level_one": "requested",
                                      "error": "provider limit"})
    with pytest.raises(PromotionError, match="cooldown"):
        controller.admit("NEW", {"price": 10})


def test_merge_is_chronological_deduplicated_and_records_rth_gaps():
    history = [
        {"symbol": "A", "timestamp": pd.Timestamp("2026-09-25T13:30:00Z"),
         "open": 1, "high": 1, "low": 1, "close": 1, "volume": 10},
        {"symbol": "A", "timestamp": pd.Timestamp("2026-09-25T13:32:00Z"),
         "open": 3, "high": 3, "low": 3, "close": 3, "volume": 10},
    ]
    stream = [{**history[1], "close": 3.5, "source": "schwab_chart_equity"}]
    merged, evidence = ManualPromotionController._merge(history, stream)
    assert [x["close"] for x in merged] == [1, 3.5]
    assert evidence == {
        "history_rows": 2, "stream_rows": 1, "merged_rows": 2,
        "deduplicated": 1, "material_disagreements": 1,
        "rth_gap_minutes": 1, "stream_won_overlap": True,
    }


def _ready_controller(*, minimum_residence_seconds=0):
    scanner, feed = _Scanner(), _Feed()
    controller = ManualPromotionController(
        scanner, feed, clock=lambda: 1_798_000_000.0,
        minimum_residence_seconds=minimum_residence_seconds,
    )
    controller.admit("NEW", {"price": 10.0})
    feed.acknowledge("NEW")
    deadline = time.time() + 2
    while controller.status("NEW")["state"] not in {"ready", "failed"} and time.time() < deadline:
        time.sleep(.01)
    return scanner, feed, controller


def test_pin_reasons_are_additive_and_one_removal_does_not_clear_another():
    _, _, controller = _ready_controller()
    controller.protect("NEW", "manual", True)
    held = controller.protect("NEW", "operator_hold", True)
    assert {item["code"] for item in held["protections"]} == {"manual", "operator_hold"}
    still_held = controller.protect("NEW", "manual", False)
    assert still_held["protected"] is True
    assert [item["code"] for item in still_held["protections"]] == ["operator_hold"]


def test_release_rejects_external_watch_warming_and_minimum_residence():
    scanner, feed = _Scanner(), _Feed()
    controller = ManualPromotionController(scanner, feed, clock=lambda: 100.0)
    controller.admit("NEW", {"price": 10.0})
    with pytest.raises(PromotionError, match="warming"):
        controller.release("NEW")
    feed.acknowledge("NEW")
    deadline = time.time() + 2
    while controller.status("NEW")["state"] != "ready" and time.time() < deadline:
        time.sleep(.01)
    feed.external_watches.add("NEW")
    with pytest.raises(PromotionError, match="external_watch"):
        controller.release("NEW")
    feed.external_watches.clear()
    with pytest.raises(PromotionError, match="minimum_residence"):
        controller.release("NEW")


def test_release_disables_before_unsubscribe_tracks_partial_ack_and_rejects_late_bars():
    scanner, feed, controller = _ready_controller()
    releasing = controller.release("NEW")
    assert scanner.releases == [("begin", "NEW", releasing["epoch"])]
    assert feed.released == ["NEW"] and releasing["setup_evaluation"] == "disabled"
    controller.on_membership("NEW", {"chart": "removed", "level_one": "removal_requested",
                                      "error": None})
    partial = controller.status("NEW")
    assert partial["state"] == "releasing" and partial["chart"] == "removed"
    assert controller.on_bar({"symbol": "NEW"}) is True
    controller.on_membership("NEW", {"chart": "removed", "level_one": "removed", "error": None})
    released = controller.status("NEW")
    assert released["state"] == "released"
    assert scanner.releases[-1] == ("finish", "NEW", releasing["epoch"])


def test_readmission_uses_fresh_epoch_after_acknowledged_release():
    _, _, controller = _ready_controller()
    first = controller.status("NEW")["epoch"]
    controller.release("NEW")
    controller.on_membership("NEW", {"chart": "removed", "level_one": "removed", "error": None})
    second = controller.admit("NEW", {"price": 10.0})
    assert second["epoch"] > first
    assert second["readiness"] is None and second["state"] == "requested"


def test_reconnect_assigns_fresh_epoch_rejects_old_bars_and_revalidates_readiness():
    scanner, feed, controller = _ready_controller()
    before = controller.status("NEW")
    controller.on_connection(False, 3)
    reconnecting = controller.status("NEW")
    assert reconnecting["state"] == "reconnecting"
    assert reconnecting["epoch"] > before["epoch"]
    assert reconnecting["setup_evaluation"] == "disabled"
    assert scanner.releases[-1] == ("reconnect", "NEW", before["epoch"], reconnecting["epoch"])
    assert controller.on_bar({"symbol": "NEW", "_dynamic_epoch": before["epoch"]}) is True
    assert controller.status("NEW")["audit"][-1]["action"] == "late_bar_rejected"

    feed.acknowledge("NEW")
    deadline = time.time() + 2
    while controller.status("NEW")["state"] not in {"ready", "failed"} and time.time() < deadline:
        time.sleep(.01)
    restored = controller.status("NEW")
    assert restored["state"] == "ready" and restored["setup_evaluation"] == "enabled"
    assert restored["epoch"] == reconnecting["epoch"]
    actions = [event["action"] for event in restored["audit"]]
    assert "connection_invalidated" in actions and actions[-1] == "readiness_confirmed"


def test_release_in_flight_completes_deterministically_after_reconnect():
    scanner, _, controller = _ready_controller()
    controller.release("NEW")
    old_epoch = controller.status("NEW")["epoch"]
    controller.on_connection(False, 4)
    reconnecting = controller.status("NEW")
    assert reconnecting["state"] == "release_reconnecting"
    assert reconnecting["epoch"] > old_epoch
    controller.on_membership("NEW", {"chart": "removed", "level_one": "removed", "error": None})
    assert controller.status("NEW")["state"] == "released"
    assert scanner.releases[-1] == ("finish", "NEW", reconnecting["epoch"])
