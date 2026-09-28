import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from scanner.schwab_screener import (
    DEFAULT_SCREENER_KEYS, SchwabScreenerBook, build_screener_key, validate_screener_key,
)
from scanner.discovery_sessions import DiscoverySessionStore


def _message():
    return {"data": [{"service": "SCREENER_EQUITY", "content": [{
        "key": "EQUITY_ALL_PERCENT_CHANGE_UP_5", "1": 1_798_000_000_000,
        "4": [
            {"symbol": "XYZ", "description": "Outside Inc", "lastPrice": 12.5,
             "netChange": 1.2, "netPercentChange": 10.62, "volume": 250_000, "trades": 3200},
            {"symbol": "AAA", "lastPrice": None, "volume": ""},
        ],
    }]}]}


def test_normalizes_rank_provenance_and_missing_values():
    clock = [1000.0]
    book = SchwabScreenerBook(clock=lambda: clock[0])
    book.requested(["EQUITY_ALL_PERCENT_CHANGE_UP_5"])
    book.connection(True)
    book.ingest(_message())
    clock[0] += 1.25
    snap = book.snapshot("EQUITY_ALL_PERCENT_CHANGE_UP_5")
    assert snap["mode"] == "observe" and snap["source"] == "schwab_screener_equity"
    assert snap["status"] == "live" and snap["receipt_age_ms"] == 1250
    expected = {
        "list_key": "EQUITY_ALL_PERCENT_CHANGE_UP_5", "rank": 1,
        "symbol": "XYZ", "description": "Outside Inc",
        "provider_timestamp": snap["provider_timestamp"], "price": 12.5,
        "net_change": 1.2, "percent_change": 10.62, "volume": 250000,
        "total_volume": None, "trades": 3200, "market_share": None,
    }
    assert {key: snap["rows"][0][key] for key in expected} == expected
    assert snap["rows"][1]["price"] is None and snap["rows"][1]["volume"] is None


def test_rejection_and_malformed_payloads_fail_open():
    book = SchwabScreenerBook()
    for malformed in (None, "not-json", {"data": [None, {"service": "SCREENER_EQUITY", "content": ["bad"]}]}):
        book.ingest(malformed)
    book.ingest({"response": [{"service": "SCREENER_EQUITY", "content": {"code": 19, "msg": "rejected"}}]})
    assert book.snapshot()["status"] == "error"
    assert book.snapshot()["error"] == "rejected"


def test_non_screener_data_is_ignored():
    book = SchwabScreenerBook()
    book.ingest({"data": [{"service": "CHART_EQUITY", "content": [{"key": "AAA", "5": 10}]}]})
    assert book.snapshot()["rows"] == []


def test_key_builder_accepts_only_supported_dimensions():
    assert build_screener_key("nasdaq", "trades", 5) == "NASDAQ_TRADES_5"
    assert validate_screener_key("equity_all_volume_0") == "EQUITY_ALL_VOLUME_0"
    with pytest.raises(ValueError, match="market"):
        build_screener_key("CRYPTO", "VOLUME", 5)
    with pytest.raises(ValueError, match="measure"):
        build_screener_key("NASDAQ", "RSI", 5)
    with pytest.raises(ValueError, match="period"):
        build_screener_key("NASDAQ", "VOLUME", 2)
    with pytest.raises(ValueError, match="key"):
        validate_screener_key("NASDAQ_VOLUME_2")


def _screen(key, stamp, *symbols):
    return {"data": [{"service": "SCREENER_EQUITY", "content": [{"key": key, "1": stamp, "4": [
        {"symbol": symbol, "lastPrice": 10 + index, "netPercentChange": index or None}
        for index, symbol in enumerate(symbols)
    ]}]}]}


def test_combined_candidates_merge_lists_ranks_and_recurrence():
    clock = [1000.0]
    book = SchwabScreenerBook(clock=lambda: clock[0])
    book.requested(DEFAULT_SCREENER_KEYS)
    book.connection(True)
    book.ingest(_screen("EQUITY_ALL_PERCENT_CHANGE_UP_5", 1000, "AAA", "DUP"))
    clock[0] += 1
    book.ingest(_screen("EQUITY_ALL_VOLUME_5", 2000, "DUP", "BBB"))
    clock[0] += 1
    book.ingest(_screen("EQUITY_ALL_PERCENT_CHANGE_UP_5", 3000, "DUP", "AAA"))
    dup = next(row for row in book.snapshot("combined")["rows"] if row["symbol"] == "DUP")
    assert dup["current_rank"] == 1 and dup["best_rank"] == 1 and dup["recurrence"] == 3
    assert [item["list_key"] for item in dup["contributing_lists"]] == [
        "EQUITY_ALL_PERCENT_CHANGE_UP_5", "EQUITY_ALL_VOLUME_5"]
    pct = next(item for item in dup["contributing_lists"] if item["list_key"].endswith("CHANGE_UP_5"))
    assert pct["current_rank"] == 1 and pct["best_rank"] == 1 and pct["recurrence"] == 2
    assert dup["first_seen"] < dup["last_seen"]


def test_stale_list_epoch_is_ignored_and_errors_are_independent():
    book = SchwabScreenerBook()
    book.requested(["EQUITY_ALL_PERCENT_CHANGE_UP_5", "EQUITY_ALL_VOLUME_5"])
    book.connection(True)
    book.ingest(_screen("EQUITY_ALL_PERCENT_CHANGE_UP_5", 2000, "NEW"))
    book.ingest(_screen("EQUITY_ALL_PERCENT_CHANGE_UP_5", 1000, "STALE"))
    book.ingest({"response": [{"service": "SCREENER_EQUITY", "key": "EQUITY_ALL_VOLUME_5",
                                "content": {"code": 19, "msg": "rejected"}}]})
    snap = book.snapshot("combined")
    assert [row["symbol"] for row in snap["rows"]] == ["NEW"]
    health = {item["list_key"]: item for item in snap["lists"]}
    assert health["EQUITY_ALL_PERCENT_CHANGE_UP_5"]["status"] == "live"
    assert health["EQUITY_ALL_VOLUME_5"]["status"] == "error"
    assert snap["status"] == "live"


def test_request_id_correlates_an_error_to_only_its_list():
    book = SchwabScreenerBook()
    book.requested(["NASDAQ_TRADES_5", "NYSE_VOLUME_5"])
    book.connection(True)
    book.expect(41, ["NASDAQ_TRADES_5"])
    book.expect(42, ["NYSE_VOLUME_5"])
    book.ingest({"response": [{"service": "SCREENER_EQUITY", "requestid": 41,
                                "content": {"code": 19, "msg": "bad list"}},
                               {"service": "SCREENER_EQUITY", "requestid": 42,
                                "content": {"code": 0, "msg": "ok"}}]})
    health = {item["list_key"]: item for item in book.snapshot()["lists"]}
    assert health["NASDAQ_TRADES_5"]["status"] == "error"
    assert health["NYSE_VOLUME_5"]["status"] == "waiting"


def test_discovery_sessions_roll_over_retain_bounded_history_and_recover_corrupt_tail(tmp_path):
    path = tmp_path / "sessions.jsonl"
    store = DiscoverySessionStore(path, retention_sessions=2)
    clock = [datetime(2026, 9, 22, 15, 55, tzinfo=ZoneInfo("America/New_York")).timestamp()]
    book = SchwabScreenerBook(clock=lambda: clock[0], session_store=store)
    book.requested(["EQUITY_ALL_PERCENT_CHANGE_UP_5"])

    for day, symbol in ((22, "AAA"), (23, "BBB"), (24, "CCC")):
        clock[0] = datetime(2026, 9, day, 15, 55,
                            tzinfo=ZoneInfo("America/New_York")).timestamp()
        book.ingest(_screen("EQUITY_ALL_PERCENT_CHANGE_UP_5", int(clock[0] * 1000), symbol))
        book.finalize("market_close")

    sessions = store.list_sessions()
    assert [item["session_date"] for item in sessions] == ["2026-09-24", "2026-09-23"]
    latest = store.get("2026-09-24")
    assert latest["status"] == "final" and latest["final_reason"] == "market_close"
    assert latest["candidates"][0]["symbol"] == "CCC"
    assert latest["candidates"][0]["eligibility"] == {"status": "not_evaluated", "reasons": []}
    assert latest["candidates"][0]["membership_decisions"] == []

    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"session_date":"truncated"')
    assert [item["session_date"] for item in store.list_sessions()] == ["2026-09-24", "2026-09-23"]


def test_active_discovery_session_recovers_and_rollover_finalizes_prior_day(tmp_path):
    store = DiscoverySessionStore(tmp_path / "sessions.jsonl", retention_sessions=20)
    clock = [datetime(2026, 9, 23, 14, 0, tzinfo=ZoneInfo("America/New_York")).timestamp()]
    first = SchwabScreenerBook(clock=lambda: clock[0], session_store=store)
    first.requested(["EQUITY_ALL_PERCENT_CHANGE_UP_5"])
    first.ingest(_screen("EQUITY_ALL_PERCENT_CHANGE_UP_5", int(clock[0] * 1000), "AAA"))

    recovered = SchwabScreenerBook(clock=lambda: clock[0], session_store=store)
    recovered.requested(["EQUITY_ALL_PERCENT_CHANGE_UP_5"])
    assert [row["symbol"] for row in recovered.snapshot()["rows"]] == ["AAA"]
    assert recovered.snapshot()["session"]["status"] == "open"

    clock[0] = datetime(2026, 9, 24, 9, 31, tzinfo=ZoneInfo("America/New_York")).timestamp()
    recovered.ingest(_screen("EQUITY_ALL_PERCENT_CHANGE_UP_5", int(clock[0] * 1000), "BBB"))
    assert store.get("2026-09-23")["status"] == "final"
    assert recovered.snapshot()["session"]["session_date"] == "2026-09-24"
    assert [row["symbol"] for row in recovered.snapshot()["rows"]] == ["BBB"]
