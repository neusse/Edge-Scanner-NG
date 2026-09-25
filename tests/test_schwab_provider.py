"""Schwab as a full data provider: rate limiting, retries, batch history,
the range-aware cache and the expired-login check. Offline (fake client)."""
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from scanner.data import schwab as sw
from scanner.data.schwab import SchwabFeed, _RateLimiter


class _Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _candles(start: date, n: int):
    return {"candles": [{"datetime": int(pd.Timestamp(start + timedelta(days=i), tz="UTC").timestamp() * 1000),
                         "open": 10, "high": 11, "low": 9, "close": 10, "volume": 1000} for i in range(n)]}


class _Client:
    def __init__(self, fail_first=0):
        self.calls, self.instrument_calls, self.fail_first = [], [], fail_first

    def price_history(self, symbol, **kw):
        self.calls.append(symbol)
        if self.fail_first:
            self.fail_first -= 1
            return _Resp(429)
        start = kw["startDate"].date()
        return _Resp(200, _candles(start, 10))

    def quotes(self, symbols, fields):
        return _Resp(200, {s: {"quote": {"lastPrice": 20.0}} for s in symbols})

    def instruments(self, symbols, projection):
        self.instrument_calls.append((symbols, projection))
        return _Resp(200, {"instruments": [
            {"symbol": symbol, "description": f"{symbol} Corp", "assetType": "EQUITY",
             "fundamental": {"marketCap": 1_000_000, "peRatio": 12.5}}
            for symbol in symbols.split(",")
        ]})


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(sw, "_LIMITER", _RateLimiter(1e9))
    monkeypatch.setattr(sw.time, "sleep", lambda s: None)


def _feed(tmp_path, client):
    return SchwabFeed(cache_dir=tmp_path / "d", intraday_cache_dir=tmp_path / "m", client=client)


def test_direct_schwab_client_strips_callback_trailing_slash(tmp_path, monkeypatch):
    """Replay initializes the provider without the live batch launcher's fixup."""
    import sys
    import types

    called = {}

    def fake_client(**kwargs):
        called.update(kwargs)
        return _Client()

    monkeypatch.setattr(sw, "refresh_token_age_days", lambda: 0.5)
    monkeypatch.setenv("SCHWAB_APP_KEY", "test-key")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "test-secret")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1/")
    monkeypatch.setitem(sys.modules, "schwabdev", types.SimpleNamespace(Client=fake_client))

    SchwabFeed(cache_dir=tmp_path / "d", intraday_cache_dir=tmp_path / "m")

    assert called["callback_url"] == "https://127.0.0.1"
    assert os.environ["SCHWAB_CALLBACK_URL"] == "https://127.0.0.1/"


def test_callback_with_trailing_slash_requires_existing_token(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "refresh_token_age_days", lambda: None)
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1/")

    with pytest.raises(RuntimeError, match="no imported schwabdev token"):
        SchwabFeed(cache_dir=tmp_path / "d", intraday_cache_dir=tmp_path / "m")


def test_callback_with_trailing_slash_blocks_implicit_relogin(tmp_path, monkeypatch):
    monkeypatch.setattr(sw, "refresh_token_age_days", lambda: 6.99)
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1/")

    with pytest.raises(RuntimeError, match="due for renewal"):
        SchwabFeed(cache_dir=tmp_path / "d", intraday_cache_dir=tmp_path / "m")


def test_rate_limiter_spaces_calls_evenly():
    t = [0.0]
    waits = []
    lim = _RateLimiter(120, clock=lambda: t[0], sleep=waits.append)
    for _ in range(4):
        lim.acquire()
    assert waits == pytest.approx([0.5, 1.0, 1.5])      # 120/min = one every 0.5 s


def test_request_retries_429_then_succeeds(tmp_path):
    c = _Client(fail_first=2)
    df = _feed(tmp_path, c).get_historical_daily("AAA", date(2026, 1, 5), date(2026, 1, 20))
    assert len(c.calls) == 3 and len(df) == 10


def test_chart_history_recovers_once_after_external_token_refresh(tmp_path):
    """A connected stream may outlive an obsolete REST Authorization header."""
    class StaleClient(_Client):
        def __init__(self):
            super().__init__()
            self.current = False
            self.refresh_calls = 0

        def price_history(self, symbol, **kw):
            self.calls.append(symbol)
            return _Resp(200, _candles(kw["startDate"].date(), 2)) if self.current else _Resp(401)

        def update_tokens(self, force_access_token=False):
            assert force_access_token is True
            self.refresh_calls += 1
            self.current = True
            return True

    client = StaleClient()
    bars = _feed(tmp_path, client).get_bars_range("AAA", "1Min", date(2026, 1, 5), date(2026, 1, 6))
    assert len(bars) == 2
    assert client.calls == ["AAA", "AAA"]
    assert client.refresh_calls == 1


def test_chart_history_does_not_loop_when_rest_auth_cannot_recover(tmp_path):
    class RejectedClient(_Client):
        def __init__(self):
            super().__init__()
            self.refresh_calls = 0

        def price_history(self, symbol, **kw):
            self.calls.append(symbol)
            return _Resp(401)

        def update_tokens(self, force_access_token=False):
            self.refresh_calls += 1
            return False

    client = RejectedClient()
    with pytest.raises(RuntimeError, match="HTTP 401"):
        _feed(tmp_path, client).get_bars_range("AAA", "1Min", date(2026, 1, 5), date(2026, 1, 6))
    assert client.calls == ["AAA"]
    assert client.refresh_calls == 1


def test_daily_multi_fetches_each_symbol_and_reports_progress(tmp_path):
    c, seen = _Client(), []
    got = _feed(tmp_path, c).get_historical_daily_multi(["A", "B", "C"], date(2026, 1, 5), date(2026, 1, 20),
                                                        progress=lambda d, n: seen.append((d, n)))
    assert sorted(got) == ["A", "B", "C"] and sorted(c.calls) == ["A", "B", "C"] and seen[-1] == (3, 3)


def test_cache_is_reused_only_when_it_covers_the_requested_span(tmp_path):
    c = _Client()
    f = _feed(tmp_path, c)
    f.get_historical_daily("AAA", date(2026, 1, 5), date(2026, 1, 20))
    f.get_historical_daily("AAA", date(2026, 1, 7), date(2026, 1, 20))     # inside: cached
    assert c.calls == ["AAA"]
    f.get_historical_daily("AAA", date(2025, 1, 5), date(2026, 1, 20))     # longer: refetch
    assert c.calls == ["AAA", "AAA"]


@pytest.mark.parametrize("timeframe,folder", [("Day", "d"), ("5Min", "m")])
def test_stale_history_fetches_only_missing_tail_and_keeps_existing_bars(tmp_path, timeframe, folder):
    """A next-day restart must not request the whole 380/45-day history again."""
    class RangeClient(_Client):
        def price_history(self, symbol, **kw):
            self.calls.append((kw["startDate"].date(), kw["endDate"].date()))
            first, last = self.calls[-1]
            candles = []
            day = first
            while day <= last:
                if day.weekday() < 5:
                    stamp = pd.Timestamp(day, tz="UTC") + pd.Timedelta(hours=15)
                    candles.append({"datetime": int(stamp.timestamp() * 1000),
                                    "open": 10, "high": 11, "low": 9,
                                    "close": 10, "volume": 1000})
                day += timedelta(days=1)
            return _Resp(200, {"candles": candles})

    client = RangeClient()
    feed = _feed(tmp_path, client)
    start, first_end, next_end = date(2026, 1, 5), date(2026, 1, 8), date(2026, 1, 9)
    if timeframe == "Day":
        first = feed.get_historical_daily("AAA", start, first_end)
    else:
        first = feed.get_historical_bars("AAA", timeframe, start, first_end)
    cache = tmp_path / folder / "AAA.parquet"
    old = time.time() - 86400 * 4
    os.utime(cache, (old, old))
    if timeframe == "Day":
        second = feed.get_historical_daily("AAA", start, next_end)
    else:
        second = feed.get_historical_bars("AAA", timeframe, start, next_end)
    assert client.calls == [(start, first_end), (first_end, next_end)]
    assert len(second) == len(first) + 1
    assert second.index.is_unique


def test_today_written_file_does_not_hide_a_missing_session(tmp_path):
    class Client(_Client):
        def price_history(self, symbol, **kw):
            self.calls.append((kw["startDate"].date(), kw["endDate"].date()))
            return _Resp(200, _candles(kw["startDate"].date(), 1))

    c = Client()
    feed = _feed(tmp_path, c)
    start = date(2026, 1, 5)
    feed.get_historical_daily("AAA", start, start)
    feed.get_historical_daily("AAA", start, date(2026, 1, 6))
    assert c.calls == [(start, start), (start, date(2026, 1, 6))]


def test_corrupt_cache_is_repaired_by_a_full_request(tmp_path):
    c = _Client()
    feed = _feed(tmp_path, c)
    path = tmp_path / "d" / "AAA.parquet"
    path.write_bytes(b"interrupted parquet write")
    feed.get_historical_daily("AAA", date(2026, 1, 5), date(2026, 1, 9))
    assert c.calls == ["AAA"]
    assert pd.read_parquet(path).shape[0] == 10


def test_snapshot_goes_through_the_limiter(tmp_path):
    assert _feed(tmp_path, _Client()).get_snapshot(["A", "B"])["B"]["price"] == 20.0


def test_fundamentals_are_batched_and_preserve_full_instrument_rows(tmp_path):
    client = _Client()
    symbols = [f"S{i:03d}" for i in range(205)]
    got = _feed(tmp_path, client).get_fundamentals(symbols)
    assert len(client.instrument_calls) == 3
    assert all(projection == "fundamental" for _, projection in client.instrument_calls)
    assert got["S204"]["fundamental"]["peRatio"] == 12.5


def _tokens_db(path, issued: datetime):
    with sqlite3.connect(path) as con:
        con.execute("create table schwabdev (access_token_issued text, refresh_token_issued text)")
        con.execute("insert into schwabdev values (?, ?)", (issued.isoformat(), issued.isoformat()))
    return path


def test_expired_login_fails_fast_with_instructions(tmp_path, monkeypatch):
    db = _tokens_db(tmp_path / "t.db", datetime.now(timezone.utc) - timedelta(days=9))
    real = sw.refresh_token_age_days
    monkeypatch.setattr(sw, "refresh_token_age_days", lambda: real(db))
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    monkeypatch.setenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1")
    with pytest.raises(RuntimeError, match="schwab_auth.py"):
        SchwabFeed(cache_dir=tmp_path / "d", intraday_cache_dir=tmp_path / "m")


def test_refresh_token_age_reads_only_the_timestamp(tmp_path):
    db = _tokens_db(tmp_path / "t.db", datetime.now(timezone.utc) - timedelta(days=3))
    assert sw.refresh_token_age_days(db) == pytest.approx(3.0, abs=0.01)
    assert sw.refresh_token_age_days(tmp_path / "missing.db") is None


def test_four_hour_bars_are_built_from_30_minute_candles(tmp_path):
    class C(_Client):
        def price_history(self, symbol, **kw):
            assert kw["frequency"] == 30
            t0 = pd.Timestamp("2026-01-05 14:30", tz="UTC")
            return _Resp(200, {"candles": [{"datetime": int((t0 + pd.Timedelta(minutes=30 * i)).timestamp() * 1000),
                                            "open": 10 + i, "high": 11 + i, "low": 9, "close": 10 + i, "volume": 100}
                                           for i in range(8)]})
    df = _feed(tmp_path, C()).get_bars_range("AAA", "4Hour", date(2026, 1, 5), date(2026, 1, 5))
    assert len(df) == 2 and df["volume"].sum() == 800 and df["high"].max() == 18


# ── Schwab's 300-symbol cap on the 1-minute bar stream ───────────────────────

def test_parse_symbol_cap_reads_schwabs_code_19():
    from scanner.data.schwab import SchwabFeed
    over = {"response": [{"service": "CHART_EQUITY", "command": "ADD", "content": {
        "code": 19, "msg": "You've reached the maximum number of symbols allowed.  (CHART_EQUITY=300, DISCARDED=250)"}}]}
    ok = {"response": [{"service": "CHART_EQUITY", "command": "ADD", "content": {"code": 0, "msg": "ADD command succeeded"}}]}
    assert SchwabFeed.parse_symbol_cap(over) == 300
    assert SchwabFeed.parse_symbol_cap(ok) is None
    assert SchwabFeed.parse_symbol_cap("not json") is None


def _fake_feed(monkeypatch, sent, quotes=None):
    """A SchwabFeed with no network: a recording stream and a canned quotes API."""
    import sys, threading, types
    from scanner.data.schwab import SchwabFeed

    class FakeStream:
        receiver = None
        def __init__(self, client): pass
        def start(self, receiver, daemon=True): FakeStream.receiver = receiver
        def chart_equity(self, keys, fields): return ("CHART_EQUITY", list(keys))
        def level_one_equities(self, keys, fields, command="ADD"): return ("LEVELONE_EQUITIES", list(keys))
        def send(self, req): sent.append(req)
        def stop(self): pass

    class FakeResp:
        status_code = 200
        def __init__(self, syms): self._syms = syms
        def json(self): return {sym: {"quote": dict(quotes or {})} for sym in self._syms}

    monkeypatch.setitem(sys.modules, "schwabdev", types.SimpleNamespace(Stream=FakeStream))
    feed = SchwabFeed.__new__(SchwabFeed)
    feed._client = types.SimpleNamespace(quotes=lambda symbols, fields: FakeResp(symbols))
    feed._stream, feed._stop_evt = None, threading.Event()
    feed.streamed_symbols, feed.unstreamed_symbols = [], []
    feed.quote_streamed_symbols, feed.polled_symbols, feed.quote_bars = [], [], None
    from scanner.quote_state import QuoteBook
    feed.quote_book = QuoteBook()
    feed.quote_covered_symbols = []
    feed._quote_lock = threading.RLock()
    feed._quote_base_symbols = set()
    feed._quote_watched_symbols = set()
    feed._stop_evt.set()                                   # return right after subscribing
    monkeypatch.setattr(feed._stop_evt, "clear", lambda: None)
    return feed, FakeStream


def test_large_universe_is_covered_in_three_tiers_in_the_order_given(monkeypatch, capsys):
    sent = []
    feed, _ = _fake_feed(monkeypatch, sent)
    symbols = [f"S{i}" for i in range(4000)]
    feed.subscribe_minute_bars(symbols, lambda bar: None)

    chart = [k for svc, keys in sent if svc == "CHART_EQUITY" for k in keys]
    quotes = [k for svc, keys in sent if svc == "LEVELONE_EQUITIES" for k in keys]
    assert chart == symbols[:300]                          # real bars: the first 300
    assert quotes == symbols[:3000]                        # same stream covers chart-bar tier too
    assert feed.quote_streamed_symbols == symbols[300:3000]
    assert feed.polled_symbols == symbols[3000:]           # polled quotes: the rest
    assert feed.unstreamed_symbols == []                   # nothing is left unscanned
    assert "Schwab coverage: 300 symbols on real 1-minute bars" in capsys.readouterr().out


def test_synthetic_bars_off_falls_back_to_the_first_300_and_says_so(monkeypatch, capsys):
    monkeypatch.setenv("SCHWAB_SYNTHETIC_BARS", "0")
    sent = []
    feed, _ = _fake_feed(monkeypatch, sent)
    symbols = [f"S{i}" for i in range(1000)]
    feed.subscribe_minute_bars(symbols, lambda bar: None)

    assert [k for svc, keys in sent if svc == "CHART_EQUITY" for k in keys] == symbols[:300]
    assert [k for svc, keys in sent if svc == "LEVELONE_EQUITIES" for k in keys] == symbols[:1000]
    assert feed.unstreamed_symbols == symbols[300:] and feed.polled_symbols == []
    assert "700 of your 1,000 symbols are NOT being scanned" in capsys.readouterr().out


def test_small_universe_uses_real_bars_only(monkeypatch):
    sent = []
    feed, _ = _fake_feed(monkeypatch, sent)
    feed.subscribe_minute_bars([f"S{i}" for i in range(120)], lambda bar: None)
    assert {svc for svc, _ in sent} == {"CHART_EQUITY", "LEVELONE_EQUITIES"}
    assert feed.quote_streamed_symbols == [] and feed.polled_symbols == []


def test_streamed_quotes_become_bars_through_the_same_callback(monkeypatch):
    from scanner.data.quote_bars import QuoteBarBuilder
    from scanner.data.schwab import SchwabFeed
    bars = []
    t = [600_000 * 60.0]
    b = QuoteBarBuilder(bars.append, lambda: t[0])
    b.track("MU", grace=3.0)
    msg = lambda **f: {"data": [{"service": "LEVELONE_EQUITIES", "content": [{"key": "MU", **f}]}]}
    SchwabFeed.handle_quotes(msg(**{"3": 100.0, "8": 5000, "10": 101.0, "11": 99.0}), b)
    t[0] += 5
    SchwabFeed.handle_quotes(msg(**{"8": 5400}), b)                    # only what changed
    t[0] += 5
    SchwabFeed.handle_quotes(msg(**{"3": 100.6, "8": 5900}), b)
    t[0] += 70
    b.flush()
    assert len(bars) == 1
    assert (bars[0]["open"], bars[0]["close"], bars[0]["volume"]) == (100.0, 100.6, 900)


def test_quote_stream_cap_moves_the_overflow_to_polling(monkeypatch):
    sent = []
    feed, stream_cls = _fake_feed(monkeypatch, sent)
    symbols = [f"S{i}" for i in range(3500)]
    feed.subscribe_minute_bars(symbols, lambda bar: None)
    stream_cls.receiver({"response": [{"service": "LEVELONE_EQUITIES", "command": "ADD", "content": {
        "code": 19, "msg": "You've reached the maximum number of symbols allowed.  (LEVELONE_EQUITIES=2000, DISCARDED=250)"}}]})
    assert feed.quote_streamed_symbols == symbols[300:2000]
    assert feed.polled_symbols == symbols[2000:]          # overflow first, then the original tail


def test_external_held_symbol_uses_existing_stream_and_budget(monkeypatch):
    sent = []
    feed, _ = _fake_feed(monkeypatch, sent)
    feed.subscribe_minute_bars(["AMD", "MU"], lambda bar: None)
    stream = feed._stream
    assert feed.watch_quotes(["HELD"])["stream_covered"] == ["HELD"]
    assert feed._stream is stream
    assert sent[-1] == ("LEVELONE_EQUITIES", ["HELD"])
    assert feed.quote_book.get("HELD")["coverage"] == "subscribing"
    assert feed.watch_quotes([])["watched"] == []
    assert feed.quote_book.get("HELD")["coverage"] == "not_watched"


def test_short_history_is_not_downloaded_again_once_that_span_was_asked_for(tmp_path):
    """A recent listing can never reach back to the start asked for. Once that
    span has been requested, what is cached is everything the provider has."""
    c = _Client()
    f = _feed(tmp_path, c)
    far_back = date(2020, 1, 1)                     # long before the fake client's first candle
    f.get_historical_daily("AAA", far_back, date(2026, 1, 20))
    f.get_historical_daily("AAA", far_back, date(2026, 1, 20))
    assert c.calls == ["AAA"]                       # second call reused the cache
    f.get_historical_daily("AAA", date(2019, 1, 1), date(2026, 1, 20))     # longer still: ask again
    assert c.calls == ["AAA", "AAA"]
    assert _feed(tmp_path, c).get_historical_daily("AAA", far_back, date(2026, 1, 20)) is not None
    assert c.calls == ["AAA", "AAA"]                # the record survives a restart
