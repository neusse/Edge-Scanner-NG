"""Small, cached adapter around yfinance's Yahoo equity screener.

Yahoo is used only to discover candidates.  The selected symbols are validated
and streamed by the configured market-data provider after the user saves them
to a watchlist and explicitly selects that watchlist as the next universe.
"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from typing import Any


PRESETS: dict[str, str] = {
    "day_gainers": "Day gainers",
    "day_losers": "Day losers",
    "most_actives": "Most active",
    "small_cap_gainers": "Small-cap gainers",
    "most_shorted_stocks": "Most shorted",
    "aggressive_small_caps": "Aggressive small caps",
    "growth_technology_stocks": "Growth technology",
    "undervalued_growth_stocks": "Undervalued growth",
    "undervalued_large_caps": "Undervalued large caps",
}

SORT_FIELDS = {
    "percentchange", "dayvolume", "avgdailyvol3m", "intradaymarketcap",
    "intradayprice", "fiftytwowkpercentchange",
}

DEFAULT_EXCHANGES = ("NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS")


class YahooScreenerError(ValueError):
    pass


def _number(value: Any) -> float | None:
    try:
        out = float(value)
        return out if out == out else None
    except (TypeError, ValueError):
        return None


def _normalize(row: dict) -> dict | None:
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol:
        return None
    return {
        "symbol": symbol,
        "name": str(row.get("shortName") or row.get("longName") or "")[:160],
        "exchange": str(row.get("exchange") or ""),
        "exchange_name": str(row.get("fullExchangeName") or ""),
        "price": _number(row.get("regularMarketPrice")),
        "change_pct": _number(row.get("regularMarketChangePercent")),
        "volume": _number(row.get("regularMarketVolume")),
        "avg_volume": _number(row.get("averageDailyVolume3Month")),
        "market_cap": _number(row.get("marketCap")),
        "fifty_two_week_high": _number(row.get("fiftyTwoWeekHigh")),
        "fifty_two_week_low": _number(row.get("fiftyTwoWeekLow")),
        "sector": str(row.get("sector") or ""),
    }


def _custom_query(filters: dict):
    try:
        from yfinance import EquityQuery
    except ImportError as exc:  # pragma: no cover - dependency is part of the project install
        raise YahooScreenerError("yfinance is not installed") from exc

    terms = [EquityQuery("eq", ["region", "us"])]
    exchanges = filters.get("exchanges") or list(DEFAULT_EXCHANGES)
    exchanges = [str(x).strip().upper() for x in exchanges if str(x).strip()]
    if exchanges:
        terms.append(EquityQuery("is-in", ["exchange", *exchanges]))

    mapping = (
        ("min_price", "gte", "intradayprice"),
        ("max_price", "lte", "intradayprice"),
        ("min_change_pct", "gte", "percentchange"),
        ("max_change_pct", "lte", "percentchange"),
        ("min_volume", "gte", "dayvolume"),
        ("min_avg_volume", "gte", "avgdailyvol3m"),
        ("min_market_cap", "gte", "intradaymarketcap"),
        ("max_market_cap", "lte", "intradaymarketcap"),
    )
    for key, op, field in mapping:
        value = _number(filters.get(key))
        if value is not None:
            terms.append(EquityQuery(op, [field, value]))
    sector = str(filters.get("sector") or "").strip()
    if sector:
        terms.append(EquityQuery("eq", ["sector", sector]))
    return EquityQuery("and", terms)


@dataclass
class _CacheEntry:
    at: float
    value: dict


class YahooScreener:
    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._cache: dict[str, _CacheEntry] = {}

    def run(self, request: dict) -> dict:
        mode = str(request.get("mode") or "preset")
        preset = str(request.get("preset") or "day_gainers")
        limit = max(1, min(250, int(request.get("limit") or 100)))
        sort_field = str(request.get("sort_field") or "percentchange")
        if sort_field not in SORT_FIELDS:
            raise YahooScreenerError("unsupported sort field")
        sort_asc = bool(request.get("sort_asc", False))
        include_otc = bool(request.get("include_otc", False))
        filters = request.get("filters") if isinstance(request.get("filters"), dict) else {}
        if mode not in ("preset", "custom"):
            raise YahooScreenerError("mode must be preset or custom")
        if mode == "preset" and preset not in PRESETS:
            raise YahooScreenerError("unknown Yahoo screener preset")

        key = json.dumps({"mode": mode, "preset": preset, "limit": limit,
                          "sort_field": sort_field, "sort_asc": sort_asc,
                          "include_otc": include_otc, "filters": filters}, sort_keys=True)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit.at < self.ttl_seconds:
                return {**hit.value, "cached": True}

        try:
            import yfinance as yf
            query = preset if mode == "preset" else _custom_query(filters)
            kwargs = {"sortField": sort_field, "sortAsc": sort_asc}
            if mode == "preset":
                raw = yf.screen(query, count=limit, **kwargs)
            else:
                raw = yf.screen(query, size=limit, **kwargs)
        except Exception as exc:
            raise YahooScreenerError(f"Yahoo screener failed: {exc}") from exc

        rows: list[dict] = []
        for raw_row in (raw or {}).get("quotes", []) or []:
            if str(raw_row.get("quoteType") or "").upper() != "EQUITY":
                continue
            if str(raw_row.get("currency") or "USD").upper() != "USD":
                continue
            exchange = str(raw_row.get("exchange") or "").upper()
            if not include_otc and exchange in ("PNK", "OQX", "OEM", "OBB", "GREY"):
                continue
            row = _normalize(raw_row)
            if row is not None:
                rows.append(row)

        value = {
            "mode": mode,
            "preset": preset if mode == "preset" else None,
            "label": PRESETS.get(preset, "Custom screen") if mode == "preset" else "Custom screen",
            "rows": rows,
            "count": len(rows),
            "total": int((raw or {}).get("total") or len(rows)),
            "limit": limit,
            "cached": False,
        }
        with self._lock:
            self._cache[key] = _CacheEntry(now, value)
        return value


__all__ = ["DEFAULT_EXCHANGES", "PRESETS", "SORT_FIELDS", "YahooScreener", "YahooScreenerError"]
