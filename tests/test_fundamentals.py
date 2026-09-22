"""Fundamentals cache: freshness window, earnings staleness and Yahoo rate limits."""
from __future__ import annotations

import scanner.fundamentals as fm


class _Ticker:
    def __init__(self, info=None, exc=None):
        self._info, self._exc = info, exc

    @property
    def info(self):
        if self._exc:
            raise self._exc
        return self._info

    def get_earnings_dates(self, limit=8):
        return None


class _RateLimit(Exception):
    pass


GOOD = {"longName": "Acme Corp", "sector": "Industrials", "website": "https://acme.example",
        "longBusinessSummary": "Makes anvils.", "floatShares": 1_000_000}


def _cache(tmp_path, factory):
    c = fm.FundamentalsCache(tmp_path / "f.json", ticker_factory=factory)
    c._wait_cooldown = lambda: None        # tests never sleep
    return c


def test_good_entry_stays_fresh_for_days(tmp_path, monkeypatch):
    c = _cache(tmp_path, lambda s: _Ticker(GOOD))
    monkeypatch.setattr(fm, "_today_et", lambda: "2026-09-14")
    c.fetch_one("ACME")
    c._symbols["ACME"]["fetched_at"] = "2026-09-14T09:00:00-04:00"
    monkeypatch.setattr(fm, "_today_et", lambda: "2026-09-18")
    assert c.get("ACME")["name"] == "Acme Corp"
    monkeypatch.setattr(fm, "_today_et", lambda: "2026-09-21")      # FRESH_DAYS later
    assert c.get("ACME") is None


def test_passed_earnings_date_makes_it_stale(tmp_path, monkeypatch):
    c = _cache(tmp_path, lambda s: _Ticker(GOOD))
    c.fetch_one("ACME")
    day = c._symbols["ACME"]["fetched_at"][:10]
    monkeypatch.setattr(fm, "_today_et", lambda: day)
    c._symbols["ACME"]["next_earnings"] = "2000-01-01"
    assert c.get("ACME") is None


def test_rate_limit_is_never_cached_and_starts_cooldown(tmp_path):
    c = _cache(tmp_path, lambda s: _Ticker(exc=_RateLimit("Too Many Requests. Rate limited.")))
    e = c.fetch_one("ACME")
    assert e["ok"] is False and e["rate_limited"] is True
    assert c.get("ACME") is None                 # asked for again, not blank all day
    assert c._cool_until > 0


def test_rate_limit_keeps_the_last_good_answer(tmp_path):
    answers = [_Ticker(GOOD), _Ticker(exc=_RateLimit("429 Too Many Requests"))]
    c = _cache(tmp_path, lambda s: answers.pop(0))
    c.fetch_one("ACME")
    e = c.fetch_one("ACME")
    assert e["ok"] is True and e["name"] == "Acme Corp"
    assert c._symbols["ACME"]["ok"] is True


def test_other_failures_are_cached_for_the_day(tmp_path):
    c = _cache(tmp_path, lambda s: _Ticker(exc=ValueError("no such symbol")))
    c.fetch_one("ZZZZ")
    e = c.get("ZZZZ")
    assert e is not None and e["ok"] is False and not e.get("rate_limited")


def test_old_rate_limited_entries_without_the_flag_are_refetched(tmp_path):
    c = _cache(tmp_path, lambda s: _Ticker(GOOD))
    c.fetch_one("ACME")
    c._symbols["ACME"].update(ok=False, error="Too Many Requests. Rate limited. Try after a while.")
    assert c.get("ACME") is None


def test_schwab_fundamentals_are_merged_and_kept_in_full(tmp_path):
    calls = []

    def fetch(symbols):
        calls.append(list(symbols))
        return {symbol: {
            "symbol": symbol, "description": f"{symbol} from Schwab", "exchange": "NYSE",
            "fundamental": {
                "marketCap": 2_500_000_000, "marketCapFloat": 8_000_000,
                "sharesOutstanding": 10_000_000, "shortIntToFloat": 7.5,
                "shortIntDayToCover": 2.25, "peRatio": 18.4, "returnOnEquity": 21.0,
            },
        } for symbol in symbols}

    c = fm.FundamentalsCache(tmp_path / "f.json", ticker_factory=lambda s: _Ticker(GOOD),
                             batch_fetcher=fetch, provider_name="schwab")
    c.prefetch_all(["AAA", "BBB"], workers=1)
    assert calls == [["AAA", "BBB"]]
    entry = c.get("AAA")
    assert entry["provider"] == "schwab"
    assert entry["market_cap"] == 2_500_000_000
    assert entry["float_shares"] == 8_000_000
    assert entry["short_pct_float"] == 0.075
    assert entry["schwab_fundamentals"]["returnOnEquity"] == 21.0


def test_configuring_schwab_invalidates_yahoo_only_cache_entry(tmp_path):
    c = _cache(tmp_path, lambda s: _Ticker(GOOD))
    c.fetch_one("ACME")
    assert c.get("ACME") is not None
    c.configure_provider(lambda symbols: {}, "schwab")
    assert c.get("ACME") is None
