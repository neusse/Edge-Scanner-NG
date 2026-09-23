"""FastAPI backend for the live dashboard.

Serves:
  - WebSocket /ws/alerts  the unified alert feed (scanner/feed_hub.py)
  - REST /api/*           bars proxy, recent alerts, universe, regime, premarket
  - /api/v2/* and /v2/    Dashboard V2 (scanner/api_v2.py)

Run by wiring AppState in run_live.py, then starting uvicorn in a daemon thread.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Optional

import pandas as pd
from fastapi import FastAPI, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse

from fastapi import Request as _Request

from scanner.feed_hub import FeedHub, Subscription

if TYPE_CHECKING:
    from scanner.data.alpaca import AlpacaFeed
    from scanner.live_scanner import LiveScanner

log = logging.getLogger(__name__)

_BAR_CACHE_TTL     = 60     # seconds before /api/bars cache expires


# ── App state (shared between scanner thread and FastAPI) ─────────────────────

class AppState:
    """Holds all shared state between the scanner and the API."""

    def __init__(
        self,
        scanner: "LiveScanner",
        feed: "AlpacaFeed",
        keep_days: int = 5,
        hub: Optional[FeedHub] = None,
    ) -> None:
        self.scanner = scanner
        self.feed = feed
        # Unified alert feed (scanner/feed_hub.py): every producer publishes here,
        # every consumer subscribes to /ws/alerts with a filter. Created here so
        # the API and the sinks share one instance; run_live.py taps the sinks.
        self.hub: FeedHub = hub or FeedHub(keep_days=keep_days)
        # bar_cache: (symbol, timeframe) -> (fetched_at, data_list)
        self._bar_cache: dict[tuple[str, str], tuple[float, list]] = {}
        self._premarket_cache: tuple[float, dict] | None = None


# ── FastAPI factory ───────────────────────────────────────────────────────────

# This is a local app with unauthenticated write endpoints, so only pages served
# from this machine may call it. Any port is fine (the Vite dev server, other
# local tools). Non-browser clients send no Origin header and are unaffected.
LOCAL_ORIGIN_RE = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"
DEFAULT_HOST = "127.0.0.1"


def bind_sockets(host: str, port: int) -> list:
    """Listening sockets for uvicorn's `Server.run(sockets=...)`.

    The default host binds BOTH loopback addresses. On Windows `localhost`
    resolves to ::1 first, and a server on 127.0.0.1 alone makes every
    `localhost` client wait about two seconds before it falls back to IPv4.
    Any other host binds just that address.
    """
    import socket
    targets = [(socket.AF_INET, "127.0.0.1"), (socket.AF_INET6, "::1")] if host == DEFAULT_HOST         else [(socket.AF_INET6 if ":" in host else socket.AF_INET, host)]
    import errno
    # "This machine has no IPv6", as opposed to "the port is taken" (10047 / 10049
    # are the Windows codes for the first two).
    no_ipv6 = {errno.EAFNOSUPPORT, errno.EADDRNOTAVAIL, getattr(errno, "EPROTONOSUPPORT", -1), 10047, 10049}
    out = []
    for family, addr in targets:
        s = None
        try:
            s = socket.socket(family, socket.SOCK_STREAM)
            if family == socket.AF_INET6:
                s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
            s.bind((addr, port))
            s.listen(128)
            s.setblocking(False)
            out.append(s)
        except OSError as exc:
            if s is not None:
                s.close()
            code = exc.errno if exc.errno is not None else getattr(exc, "winerror", None)
            if family == socket.AF_INET6 and out and code in no_ipv6:
                log.warning("IPv6 loopback unavailable (%s): serving on 127.0.0.1 only. "
                            "Clients using `localhost` may connect slowly.", exc)
                continue
            for o in out:
                o.close()
            raise OSError(exc.errno, f"cannot listen on [{addr}]:{port}: {exc.strerror or exc}. "
                                     "Is another scanner, or another program, already using that port?") from exc
    return out


def origin_is_local(origin: Optional[str], host: Optional[str] = None) -> bool:
    """True for a missing Origin (scripts, bots), one on this machine, or the
    page this server itself served (same host:port, which covers --host on a LAN)."""
    import re
    if not origin or re.match(LOCAL_ORIGIN_RE, origin) is not None:
        return True
    return bool(host) and origin.split("://", 1)[-1] == host


def _premarket_result(app_state: "AppState", items: list[dict], pm_keep: dict, symbols_total: int) -> dict:
    """Rank the premarket items into the three lists, cache and return the payload."""
    import datetime as _dt
    from zoneinfo import ZoneInfo
    et = ZoneInfo("America/New_York")

    def _scoped(name: str) -> list[dict]:
        k = pm_keep.get(name)
        return items if k is None else [x for x in items if k(x["symbol"])]

    gainers = sorted([x for x in _scoped("pm_gainers") if x["change_pct"] > 0],
                     key=lambda x: x["change_pct"], reverse=True)[:25]
    losers  = sorted([x for x in _scoped("pm_losers") if x["change_pct"] < 0],
                     key=lambda x: x["change_pct"])[:25]
    volume  = sorted(_scoped("pm_volume"),
                     key=lambda x: x["premarket_volume"], reverse=True)[:25]
    now_et = _dt.datetime.now(et)
    result = {
        "gainers":        gainers,
        "losers":         losers,
        "volume":         volume,
        "symbols_total":  symbols_total,
        "symbols_active": len(items),
        "fetched_at":     now_et.strftime("%H:%M:%S ET"),
        "session_date":   now_et.strftime("%Y-%m-%d"),
    }
    app_state._premarket_cache = (time.monotonic(), result)
    return result


def create_app(app_state: AppState) -> FastAPI:
    """Build and return the FastAPI application."""
    app = FastAPI(title="Scanner Dashboard API")

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=LOCAL_ORIGIN_RE,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Startup: launch WS broadcaster ───────────────────────────────────────

    @app.on_event("startup")
    async def _start_broadcaster() -> None:
        asyncio.create_task(app_state.hub.broadcast_loop())      # unified feed
        log.info("API: WebSocket broadcasters started")

    # ── WebSocket endpoint: the unified feed ─────────────────────────────────
    # /ws/alerts                              every source (system, custom)
    # /ws/alerts?sources=system&setups=A,B    server-side filter
    # Filters: sources, setups, triggers, symbols, direction, min_score, custom.
    # Frames: {"type":"replay","alerts":[...],"filter":{...}} then {"type":"alert","alert":{...}}.

    @app.websocket("/ws/alerts")
    async def ws_alerts(ws: WebSocket) -> None:
        # CORS does not cover WebSockets, so check the Origin here.
        if not origin_is_local(ws.headers.get("origin"), ws.headers.get("host")):
            await ws.close(code=1008)
            return
        await app_state.hub.serve(ws, ws.query_params)

    @app.get("/api/alerts")
    async def get_alerts(request: _Request, limit: int = 200) -> JSONResponse:
        """Recent alerts from the unified feed, newest first, same filters as the WebSocket."""
        try:
            sub = Subscription.from_params(request.query_params)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"alerts": app_state.hub.recent_for(sub, max(1, min(limit, 5000))), "filter": sub.describe()})

    @app.get("/api/alerts/recover")
    async def recover_alerts(request: _Request, after: str | None = None,
                             limit: int = 500) -> JSONResponse:
        """Oldest-first retained archive page after an event ID (410 if expired)."""
        try:
            sub = Subscription.from_params(request.query_params)
            result = app_state.hub.recover_for(sub, after, max(1, min(limit, 500)))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except LookupError:
            return JSONResponse({"error": "cursor_not_found", "after": after}, status_code=410)
        return JSONResponse(result)

    @app.get("/api/feed/status")
    async def get_feed_status() -> JSONResponse:
        return JSONResponse(app_state.hub.status_snapshot())

    @app.get("/api/feed/clients")
    async def get_feed_clients() -> JSONResponse:
        """Who is subscribed to the unified feed and with which filter."""
        return JSONResponse({"clients": app_state.hub.clients(), "published": app_state.hub.published})

    # ── REST: bars proxy ──────────────────────────────────────────────────────

    @app.get("/api/bars/{symbol}/{timeframe}")
    async def get_bars(symbol: str, timeframe: str) -> JSONResponse:
        """Proxy Alpaca bars with a 60-second memory cache.

        timeframe: "5min" (today's intraday) or "1day" (historical daily)
        """
        symbol = symbol.upper()
        cache_key = (symbol, timeframe)
        now = time.monotonic()

        cached = app_state._bar_cache.get(cache_key)
        if cached and (now - cached[0]) < _BAR_CACHE_TTL:
            return JSONResponse({"bars": cached[1]})

        try:
            from datetime import date, timedelta
            # Intraday frames carry several days so a 200-period SMA exists on the
            # chart's own bars (1min: ~3 sessions, 5min: ~7, 15min: ~17).
            # (feed timeframe, calendar days of history)
            _RANGE_TF = {"1min": ("1Min", 5), "5min": ("5Min", 10), "15min": ("15Min", 25),
                         "30min": ("30Min", 45), "1hour": ("1Hour", 90), "4hour": ("4Hour", 200)}
            if timeframe in _RANGE_TF:
                _tf, _days = _RANGE_TF[timeframe]
                end   = date.today()
                start = end - timedelta(days=_days)
                df = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: app_state.feed.get_bars_range(symbol, _tf, start, end)
                )
            elif timeframe == "1day":
                end   = date.today()
                start = end - timedelta(days=600)   # ~410 sessions: enough for a daily SMA200 series
                df = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: app_state.feed.get_bars_range(symbol, "Day", start, end)
                )
            elif timeframe == "1week":
                end   = date.today()
                start = end - timedelta(days=1095)  # ~3 years
                df = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: app_state.feed.get_bars_range(symbol, "1Week", start, end)
                )
            else:
                return JSONResponse({"error": f"unknown timeframe: {timeframe}"}, status_code=400)

            bars = _df_to_bars(df)
            app_state._bar_cache[cache_key] = (now, bars)
            return JSONResponse({"bars": bars})

        except Exception as exc:
            log.warning("API: bars fetch error %s/%s: %s", symbol, timeframe, exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

    # ── REST: universe ────────────────────────────────────────────────────────

    @app.get("/api/universe")
    async def get_universe() -> JSONResponse:
        symbols = sorted(app_state.scanner._states.keys())
        return JSONResponse({"symbols": symbols, "count": len(symbols)})

    # ── REST: regime ──────────────────────────────────────────────────────────

    @app.get("/api/regime")
    async def get_regime() -> JSONResponse:
        return JSONResponse({"regime": app_state.scanner._regime.value})

    # ── REST: pre-market scanner ─────────────────────────────────────────────

    @app.get("/api/premarket")
    async def get_premarket() -> JSONResponse:
        """Return top gainers, losers, and volume from pre-market bars (4–9:29 ET).

        Only universe symbols are included.  60-second server-side cache.
        """
        import datetime as _dt
        from zoneinfo import ZoneInfo
        _ET = ZoneInfo("America/New_York")

        cached = app_state._premarket_cache
        if cached and (time.monotonic() - cached[0]) < 60:
            return JSONResponse(cached[1])

        states  = app_state.scanner._states
        symbols = list(states.keys())
        if not symbols:
            return JSONResponse({"gainers": [], "losers": [], "volume": [],
                                 "symbols_total": 0, "symbols_active": 0})

        # Universe filters for the three lists (Config > Toplists). Each list
        # has its own, so narrow the FETCH to the union and filter each list
        # from the fetched set afterwards. When all three share one filter that
        # turns a 6,455-symbol bar fetch into a few hundred.
        from scanner.toplists import PREMARKET_LISTS, member_predicate
        eng = getattr(app_state.scanner, "_profiles", None)
        pm_keep = {n: member_predicate(eng, n)[0] for n in PREMARKET_LISTS}
        if all(k is not None for k in pm_keep.values()):
            wanted = {s for s in symbols if any(k(s) for k in pm_keep.values())}
            if wanted:
                symbols = [s for s in symbols if s in wanted]

        prior_closes = {
            sym: getattr(st, "prior_close", None)
            for sym, st in states.items()
        }

        # A provider with no batch history endpoint (Schwab: one request per
        # symbol, about 120 a minute) cannot re-download today's bars for the
        # whole universe on every refresh: the screen stays blank for minutes and
        # the requests starve its live quote polling. The scanner already holds
        # the premarket price and volume of every symbol from the bars it has
        # processed, so such a provider is served from that, with no requests.
        if getattr(app_state.feed, "per_symbol_history", False):
            items = []
            for sym in symbols:
                st = states.get(sym)
                last, vol, prev = getattr(st, "pm_last", None), getattr(st, "pm_vol", 0.0), prior_closes.get(sym)
                if st is None or last is None or not vol:
                    continue
                items.append({
                    "symbol":           sym,
                    "price":            round(float(last), 2),
                    "change_pct":       round((last - prev) / prev * 100, 2) if prev and prev > 0 else 0.0,
                    "premarket_volume": int(vol),
                    "prev_close":       round(float(prev), 2) if prev else None,
                })
            return JSONResponse(_premarket_result(app_state, items, pm_keep, len(symbols)))

        try:
            bars_map = await asyncio.get_event_loop().run_in_executor(
                None, lambda: app_state.feed.get_todays_bars_multi(symbols, "5Min")
            )
        except Exception as exc:
            log.warning("premarket: fetch error: %s", exc)
            return JSONResponse({"error": str(exc)}, status_code=500)

        items: list[dict] = []
        for sym, df in bars_map.items():
            if df is None or df.empty:
                continue
            try:
                df_et = df.copy()
                if hasattr(df_et.index, "tz_convert"):
                    df_et.index = df_et.index.tz_convert(_ET)
                pm = df_et[
                    (df_et.index.hour >= 4) &
                    ((df_et.index.hour < 9) |
                     ((df_et.index.hour == 9) & (df_et.index.minute < 30)))
                ]
                if pm.empty:
                    continue
                last_price = float(pm["close"].iloc[-1])
                pm_volume  = int(pm["volume"].sum())
                prev_close = prior_closes.get(sym)
                change_pct = (
                    (last_price - prev_close) / prev_close * 100
                    if prev_close and prev_close > 0 else 0.0
                )
                items.append({
                    "symbol":           sym,
                    "price":            round(last_price, 2),
                    "change_pct":       round(change_pct, 2),
                    "premarket_volume": pm_volume,
                    "prev_close":       round(float(prev_close), 2) if prev_close else None,
                })
            except Exception:
                continue

        return JSONResponse(_premarket_result(app_state, items, pm_keep, len(symbols)))

    # Routes contributed by an optional engine plugin (scanner/plugins.py).
    from scanner import plugins
    ext = plugins.api_extension()
    if ext is not None:
        ext.register(app, app_state)

    # Dashboard V2: /api/v2/* + /v2 static (scanner/api_v2.py).
    from scanner.api_v2 import register_v2_routes, mount_v2_static
    register_v2_routes(app, app_state)
    mount_v2_static(app)

    @app.get("/", include_in_schema=False)
    async def _root() -> RedirectResponse:
        return RedirectResponse("/v2/")

    return app


def _sanitize(obj: object) -> object:
    """Recursively replace nan/inf floats with None so json.dumps never raises."""
    import math
    if isinstance(obj, float):
        return None if not math.isfinite(obj) else obj
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(v) for v in obj]
    return obj


def _df_to_bars(df: pd.DataFrame) -> list[dict]:
    """Convert an OHLCV DataFrame to a list of {t, o, h, l, c, v} dicts."""
    if df is None or df.empty:
        return []
    rows = []
    for ts, row in df.iterrows():
        t = ts
        if hasattr(t, "tz_convert"):
            t = t.tz_convert("America/New_York")
        rows.append({
            "t": t.isoformat() if hasattr(t, "isoformat") else str(t),
            "o": float(row.get("open",  0)),
            "h": float(row.get("high",  0)),
            "l": float(row.get("low",   0)),
            "c": float(row.get("close", 0)),
            "v": float(row.get("volume", 0)),
        })
    return rows
