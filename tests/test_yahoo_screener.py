from types import SimpleNamespace

import pytest

from scanner.yahoo_screener import YahooScreener, YahooScreenerError


class FakeEquityQuery:
    def __init__(self, operator, operands):
        self.operator = operator
        self.operands = operands


def _quote(symbol="AAPL", exchange="NMS"):
    return {"symbol": symbol, "quoteType": "EQUITY", "currency": "USD", "exchange": exchange,
            "shortName": "Apple", "regularMarketPrice": 200, "regularMarketChangePercent": 2.5,
            "regularMarketVolume": 1_000_000, "averageDailyVolume3Month": 900_000,
            "marketCap": 3_000_000_000_000}


def test_preset_normalizes_filters_and_caches(monkeypatch):
    calls = []
    def screen(query, **kwargs):
        calls.append((query, kwargs))
        return {"quotes": [_quote(), _quote("OTC", "PNK"), {**_quote("ETF"), "quoteType": "ETF"}], "total": 3}
    monkeypatch.setitem(__import__('sys').modules, "yfinance", SimpleNamespace(screen=screen, EquityQuery=FakeEquityQuery))
    scanner = YahooScreener(ttl_seconds=60)
    request = {"mode": "preset", "preset": "day_gainers", "limit": 25}
    first = scanner.run(request)
    second = scanner.run(request)
    assert [r["symbol"] for r in first["rows"]] == ["AAPL"]
    assert first["cached"] is False and second["cached"] is True and len(calls) == 1


def test_custom_builds_query_and_rejects_bad_requests(monkeypatch):
    seen = {}
    def screen(query, **kwargs):
        seen["query"], seen["kwargs"] = query, kwargs
        return {"quotes": [_quote()], "total": 1}
    monkeypatch.setitem(__import__('sys').modules, "yfinance", SimpleNamespace(screen=screen, EquityQuery=FakeEquityQuery))
    result = YahooScreener().run({"mode": "custom", "filters": {"min_price": 5, "min_volume": 100000}, "limit": 50})
    assert result["count"] == 1 and seen["query"].operator == "and" and seen["kwargs"]["size"] == 50
    with pytest.raises(YahooScreenerError, match="unknown"):
        YahooScreener().run({"mode": "preset", "preset": "not_real"})
    with pytest.raises(YahooScreenerError, match="sort field"):
        YahooScreener().run({"sort_field": "DROP TABLE"})
