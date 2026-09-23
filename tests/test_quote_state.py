"""Public quote contract, exercised without a broker connection."""
import time

from scanner.quote_state import QuoteBook
from scanner.data.schwab import SchwabFeed


T = 1_780_000_000_000


def test_quote_only_sparse_updates_keep_each_side_market_time():
    book = QuoteBook(stale_after_ms=2000)
    book.cover("AMD")
    book.ingest("AMD", {"bid": (100, T, 4), "ask": (100.02, T, 5),
                        "last": (100.01, T, 10)}, receipt_ms=T + 50, delayed=False)
    row = book.ingest("AMD", {"ask": (100.04, T + 100, None)}, receipt_ms=T + 150)
    assert row["bid"] == 100 and row["ask"] == 100.04
    assert row["bid_market_ms"] == T and row["ask_market_ms"] == T + 100
    assert row["last_market_ms"] == T and row["bid_size"] == 4
    assert row["spread_bps"] > 0
    assert book.get("AMD", now_ms=T + 2200)["quality"] == "stale"


def test_missing_invalid_locked_crossed_delayed_out_of_order():
    book = QuoteBook(stale_after_ms=5000)
    assert book.ingest("MU", {"bid": (100, T, 1)}, receipt_ms=T, delayed=False)["quality"] == "missing"
    assert book.ingest("MU", {"ask": (0, T, 1)}, receipt_ms=T)["quality"] == "invalid"
    assert book.ingest("MU", {"ask": (100, T + 1, 1)}, receipt_ms=T + 1)["quality"] == "locked"
    assert book.ingest("MU", {"ask": (99, T + 2, 1)}, receipt_ms=T + 2)["quality"] == "crossed"
    assert book.ingest("MU", {"ask": (101, T + 3, 1)}, receipt_ms=T + 3, delayed=True)["quality"] == "delayed"
    assert book.ingest("MU", {"ask": (95, T + 2, 1)}, receipt_ms=T + 4)["ask"] == 101
    assert book.get("MU", now_ms=T + 4)["spread"] == 1


def test_history_and_update_rings_are_bounded_and_gap_is_reported():
    book = QuoteBook(history_per_symbol=3, max_history_total=3, max_updates=4)
    for i in range(10):
        book.ingest("WDC", {"bid": (100 + i, T + i * 1000, 1)}, receipt_ms=T + i * 1000)
    assert len(book.history("WDC")) == 3
    updates = book.updates(0)
    assert len(updates["updates"]) == 4 and updates["gap"]
    assert [u["bid"] for u in updates["updates"]] == [106, 107, 108, 109]
    assert not book.updates(6)["gap"]

    book.ingest("MU", {"bid": (99, T + 10_000, 1)}, receipt_ms=T + 10_000)
    assert len(book.history("WDC")) + len(book.history("MU")) == 3


def test_levelone_without_trade_updates_book_but_not_bar_builder():
    book = QuoteBook()
    class Builder:
        calls = []
        def on_quote(self, *args, **kwargs):
            self.calls.append((args, kwargs))
    builder = Builder()
    raw = {"data": [{"service": "LEVELONE_EQUITIES", "timestamp": T + 100,
                     "content": [{"key": "AMD", "delayed": False,
                                  "1": 100, "2": 100.03, "4": 30, "5": 40,
                                  "37": T, "38": T}]}]}
    SchwabFeed.handle_quotes(raw, builder, book)
    assert book.get("AMD", now_ms=T + 500)["spread"] == 0.03 or abs(book.get("AMD", now_ms=T + 500)["spread"] - 0.03) < 1e-8
    assert book.get("AMD", now_ms=T + 500)["last"] is None
    assert builder.calls[0][1]["total_volume"] is None


def test_side_timestamp_only_update_does_not_erase_price():
    book = QuoteBook()
    book.ingest("AMD", {"bid": (100, T, 1)}, receipt_ms=T)
    row = book.ingest("AMD", {"bid": (None, T + 100, None)}, receipt_ms=T + 100)
    assert row["bid"] == 100 and row["bid_market_ms"] == T + 100


def test_disconnect_and_next_session_never_reuse_old_quote_as_valid():
    book = QuoteBook(stale_after_ms=2000)
    book.ingest("AMD", {"bid": (100, T, 1), "ask": (100.02, T, 1)},
                receipt_ms=T + 10, delayed=False)
    book.connection(False)
    assert book.get("AMD", now_ms=T + 100)["quality"] == "unavailable"
    book.connection(True)
    assert book.get("AMD", now_ms=T + 100)["coverage"] == "subscribing"
    assert book.get("AMD", now_ms=T + 86_400_000)["quality"] != "valid"
    row = book.ingest("AMD", {"ask": (100.03, T + 86_400_000, 1)},
                      receipt_ms=T + 86_400_010, delayed=False)
    assert row["quality"] == "stale"  # yesterday's bid is still yesterday's bid
