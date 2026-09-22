"""Dashboard V2 routes: everything under /api/v2/* plus the /v2 static mount.

Wired from scanner/api.py::create_app() with three lines placed BEFORE the
old dashboard's static mount (that mount owns "/" and its SPA fallback swallows
any unknown path, so v2 routes must be registered first and must live under
/api/ or /v2/).

Nothing here touches scanning logic. Reads of the live SymbolState map happen
on uvicorn threads while the scanner thread mutates it, so every read copies
`list(scanner._states.items())` once and every per-row build is wrapped: a torn
read drops a row, never 500s. `_last_close` is private to SymbolState and is
read in exactly one place, `price_of()`, guarded by a test.
"""
from __future__ import annotations

import asyncio
import csv
import logging
import math
import os
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd
from fastapi import Body, FastAPI, Query
from fastapi.responses import FileResponse, JSONResponse

from scanner import plugins
from scanner.custom_setups import CustomEvaluator, CustomSetupStore, SetupError, SetupNames, summary_lines
from scanner.events import EventBuffer
from scanner.recent_activity import build_setup_check
from scanner.fundamentals import FundamentalsCache, get_cache as get_fundamentals_cache
from scanner.json_store import LayoutStore, UniverseSelectionStore, WatchlistStore, sanitize_id
from scanner.conditions import catalog_json as conditions_catalog_json
from scanner.news import NewsClient
from scanner.profiles import (
    ALL_ID,
    ProfileEngine,
    ProfileError,
    profile_stats,
    summary_lines as profile_summary_lines,
)
from scanner.settings import gate_stats, settings
from scanner.toplists import (
    FILTER_SCOPE, FILTERABLE_LISTS, TOPLIST_LABEL, TOPLISTS, ToplistEngine,
    assignment_key, member_predicate,
)
from scanner.trigger_catalog import catalog_json
from scanner.universe_selection import stream_symbols
from scanner.yahoo_screener import PRESETS as YAHOO_PRESETS, YahooScreener, YahooScreenerError

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")
_REPO = Path(__file__).parent.parent
_V2_DIST = _REPO / "dashboard-v2" / "dist"
_UNIVERSE_CSV = Path("data/universe.csv")
_SECTOR_CSV = Path("data/sector_map.csv")
_SCHWAB_CHART_CAP = 300
_SUPPORT_RESERVE = 12                  # SPY + the eleven sector ETFs
_SAFE_WATCHLIST_CAP = _SCHWAB_CHART_CAP - _SUPPORT_RESERVE


# ── helpers ──────────────────────────────────────────────────────────────────

def clean(obj: Any) -> Any:
    """JSON-safe copy: numpy scalars -> python, NaN/inf -> None, Timestamps -> iso."""
    if obj is None or isinstance(obj, (str, bool)):
        return obj
    if isinstance(obj, (int,)) and not isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [clean(v) for v in obj]
    if hasattr(obj, "isoformat"):
        try:
            return obj.isoformat()
        except Exception:
            return str(obj)
    if hasattr(obj, "item"):          # numpy scalar
        try:
            return clean(obj.item())
        except Exception:
            return None
    try:
        f = float(obj)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return str(obj)


def price_of(state) -> Optional[float]:
    """Current price of a SymbolState. The ONLY reader of the private `_last_close`."""
    v = getattr(state, "_last_close", None)
    if v is None:
        v = getattr(state, "prior_close", None)
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return (a - b) / b * 100.0


def _g(state, name: str):
    try:
        return getattr(state, name)
    except Exception:
        return None


def session_now(now: Optional[datetime] = None) -> tuple[str, Optional[datetime]]:
    """('pre'|'rth'|'post'|'closed', next boundary in ET). No holiday calendar."""
    now = now.astimezone(_ET) if now else datetime.now(_ET)
    d = now.date()
    t = now.time()
    def at(day, hh, mm):
        return datetime.combine(day, dtime(hh, mm), tzinfo=_ET)
    if now.weekday() >= 5:
        nxt = d + timedelta(days=(7 - now.weekday()))       # next Monday
        return "closed", at(nxt, 4, 0)
    if t < dtime(4, 0):
        return "closed", at(d, 4, 0)
    if t < dtime(9, 30):
        return "pre", at(d, 9, 30)
    if t < dtime(16, 0):
        return "rth", at(d, 16, 0)
    if t < dtime(20, 0):
        return "post", at(d, 20, 0)
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return "closed", at(nxt, 4, 0)


def state_to_snapshot(state) -> dict:
    price = price_of(state)
    return {
        "price": price,
        "prior_close": _g(state, "prior_close"),
        "chg_pct": _pct(price, _g(state, "prior_close")),
        "rth_chg_pct": _g(state, "rth_chg_pct"),
        "rvol": _g(state, "rvol"),
        "openingDirection5m": _g(state, "opening_candle_direction"),
        "openingRvol5m": _g(state, "opening_rvol_m5"),
        "openingRvolRank": _g(state, "opening_rvol_rank"),
        "openingRvolCoverage": _g(state, "opening_rvol_coverage"),
        "vwap": _g(state, "vwap"),
        "dist_vwap_pct": _g(state, "dist_vwap_pct"),
        "hod": _g(state, "high_of_day"),
        "lod": _g(state, "low_of_day"),
        "gap_pct": _g(state, "gap_pct"),
    }


def state_to_info(state, meta: Optional[dict], sector_etf: Optional[str]) -> dict:
    price = price_of(state)
    return {
        "found": True,
        "symbol": getattr(state, "symbol", None),
        "as_of": datetime.now(_ET).isoformat(timespec="seconds"),
        "price": price,
        "prior_close": _g(state, "prior_close"),
        "prior_high": _g(state, "prior_high"),
        "prior_low": _g(state, "prior_low"),
        "prior_open": _g(state, "prior_open"),
        "chg_pct": _pct(price, _g(state, "prior_close")),
        "rth_chg_pct": _g(state, "rth_chg_pct"),
        "gap_pct": _g(state, "gap_pct"),
        "prior_day_chg_pct": _g(state, "prior_day_chg_pct"),
        "session_open": _g(state, "session_open"),
        "hod": _g(state, "high_of_day"),
        "lod": _g(state, "low_of_day"),
        "pm_high": _g(state, "pm_high"),
        "pm_low": _g(state, "pm_low"),
        "pm_vol": _g(state, "pm_vol"),
        "vwap": _g(state, "vwap"),
        "dist_vwap_pct": _g(state, "dist_vwap_pct"),
        "rvol": _g(state, "rvol"),
        "openingDirection5m": _g(state, "opening_candle_direction"),
        "openingRvol5m": _g(state, "opening_rvol_m5"),
        "openingRvolRank": _g(state, "opening_rvol_rank"),
        "openingRvolCoverage": _g(state, "opening_rvol_coverage"),
        "adv20": _g(state, "adv20"),
        "mom_15m_pct": _g(state, "mom_15m_pct"),
        "day_range_pos": _g(state, "day_range_pos"),
        "ema9": _g(state, "ema_9"),
        "ema21": _g(state, "ema_21"),
        "dist_ema9_pct": _g(state, "dist_ema9_pct"),
        "vwap_crosses_30m": _g(state, "vwap_crosses_30m"),
        "sma50": _g(state, "sma_50"),
        "sma100": _g(state, "sma_100"),
        "sma200": _g(state, "sma_200"),
        "ema8_d1": _g(state, "ema_8_d1"),
        "atr_d1": _g(state, "atr_d1"),
        "atr14_d1": _g(state, "atr_14_d1"),
        "rrs_d1": _g(state, "rrs_d1"),
        "rrs_sector_d1": _g(state, "rrs_sector_d1"),
        "rrs_sector_m5": _g(state, "rrs_sector_m5"),
        "chart_quality": _g(state, "chart_quality"),
        "sector_etf": sector_etf,
        "universe": meta,
    }


def load_universe_meta(universe_csv: Path = _UNIVERSE_CSV, sector_csv: Path = _SECTOR_CSV) -> dict[str, dict]:
    """symbol -> {last_price, avg_vol_20d, avg_dollar_vol_20d, atr_pct, sector_etf}. Tolerates missing files."""
    meta: dict[str, dict] = {}
    try:
        with open(universe_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sym = (row.get("symbol") or "").strip().upper()
                if not sym:
                    continue
                m: dict = {}
                for k in ("last_price", "avg_vol_20d", "avg_dollar_vol_20d", "atr_pct"):
                    try:
                        m[k] = float(row[k]) if row.get(k) not in (None, "") else None
                    except (TypeError, ValueError):
                        m[k] = None
                meta[sym] = m
    except OSError:
        log.info("api_v2: %s not found; universe meta empty", universe_csv)
    try:
        with open(sector_csv, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                sym = (row.get("symbol") or "").strip().upper()
                if sym:
                    meta.setdefault(sym, {})["sector_etf"] = (row.get("sector_etf") or "").strip() or None
    except OSError:
        pass
    return meta


# ── state ────────────────────────────────────────────────────────────────────

class V2State:
    def __init__(
        self,
        app_state,
        *,
        layouts_dir: Optional[Path] = None,
        watchlists_path: Optional[Path] = None,
        universe_selection_path: Optional[Path] = None,
        fundamentals_path: Optional[Path] = None,
        news_client: Optional[NewsClient] = None,
        universe_csv: Path = _UNIVERSE_CSV,
        sector_csv: Path = _SECTOR_CSV,
        setups_dir: Optional[Path] = None,
        yahoo_screener: Optional[YahooScreener] = None,
    ) -> None:
        self.app_state = app_state
        self.layouts = LayoutStore(layouts_dir) if layouts_dir else LayoutStore()
        self.watchlists = WatchlistStore(watchlists_path) if watchlists_path else WatchlistStore()
        self.universe_selection = (
            UniverseSelectionStore(universe_selection_path)
            if universe_selection_path else UniverseSelectionStore()
        )
        self.yahoo_screener = yahoo_screener or YahooScreener()
        self.fundamentals: FundamentalsCache = (
            get_fundamentals_cache(fundamentals_path) if fundamentals_path else get_fundamentals_cache()
        )
        self.news = news_client or NewsClient(os.environ.get("ALPACA_API_KEY"), os.environ.get("ALPACA_SECRET_KEY"))
        self.toplists = ToplistEngine(app_state.scanner)
        # run_live.py creates the buffer before the scanner starts so the HOD/LOD
        # hook and this API share it; tests get a fresh one.
        buf = getattr(app_state, "event_buffer", None)
        self.events: EventBuffer = buf if isinstance(buf, EventBuffer) else EventBuffer()
        app_state.event_buffer = self.events
        self.universe_meta = load_universe_meta(universe_csv, sector_csv)
        # Custom setups. The launcher (scripts/run_live.py) attaches a CustomEvaluator to the
        # scanner and expose it as app_state.custom_eval AFTER create_app(), so it
        # is looked up lazily (see the custom_eval property). The store here and
        # the evaluator's store point at the same directory; saves go through
        # this one and then hot-reload the evaluator.
        self._setups_dir = setups_dir
        self._setups: Optional[CustomSetupStore] = None     # built lazily, only without an evaluator
        self.setup_names = SetupNames(setups_dir / "names.json") if setups_dir else SetupNames()
        # Universe profiles. Same arrangement as the custom evaluator: the
        # launcher attaches a ProfileEngine to the scanner, so look it up lazily
        # and fall back to a standalone one for tests and headless use.
        self._profile_engine: Optional[ProfileEngine] = None

    @property
    def custom_eval(self) -> Optional[CustomEvaluator]:
        ev = getattr(self.app_state, "custom_eval", None)
        return ev if isinstance(ev, CustomEvaluator) else None

    @property
    def profiles(self) -> ProfileEngine:
        """The live engine when the scanner has one, else a standalone."""
        eng = getattr(getattr(self.app_state, "scanner", None), "_profiles", None)
        if isinstance(eng, ProfileEngine):
            return eng
        if self._profile_engine is None:
            self._profile_engine = ProfileEngine()
        return self._profile_engine

    @property
    def profiles_live(self) -> bool:
        return isinstance(
            getattr(getattr(self.app_state, "scanner", None), "_profiles", None), ProfileEngine)

    @property
    def setups(self) -> CustomSetupStore:
        ev = self.custom_eval
        if ev is not None:
            return ev.store
        if self._setups is None:
            self._setups = CustomSetupStore(self._setups_dir / "custom") if self._setups_dir else CustomSetupStore()
        return self._setups


# ── routes ───────────────────────────────────────────────────────────────────

def register_v2_routes(app: FastAPI, app_state, **state_kw) -> V2State:
    v2 = V2State(app_state, **state_kw)
    app_state.v2 = v2
    scanner = app_state.scanner

    def states_copy() -> list[tuple[str, Any]]:
        try:
            return list(scanner._states.items())
        except Exception:
            return []

    def sector_of(sym: str) -> Optional[str]:
        try:
            smap = getattr(scanner, "_sector_map", None) or {}
            return smap.get(sym) or (v2.universe_meta.get(sym) or {}).get("sector_etf")
        except Exception:
            return None

    @app.get("/api/v2/clock")
    async def v2_clock() -> JSONResponse:
        now = datetime.now(_ET)
        # Replay mode (app_state.replay set by a replay driver): the clock follows the replay
        # cursor so the dashboard reads the replayed session, not wall time.
        replay = None
        rp = getattr(app_state, "replay", None)
        if isinstance(rp, dict):
            cur = rp.get("cursor")
            if cur is not None:
                try:
                    now = pd.Timestamp(cur).tz_convert(_ET).to_pydatetime()
                except Exception:
                    pass
            replay = {"date": rp.get("date"), "speed": rp.get("speed"),
                      "cursor_et": now.isoformat(timespec="seconds") if rp.get("cursor") is not None else None}
        session, nxt = session_now(now)
        regime = getattr(getattr(scanner, "_regime", None), "value", None) or "neutral"
        spy = None
        spy_state = getattr(scanner, "_spy_state", None)
        if spy_state is not None:
            try:
                p = price_of(spy_state)
                spy = {"price": p, "vwap": _g(spy_state, "vwap"), "chg_pct": _pct(p, _g(spy_state, "prior_close"))}
            except Exception:
                spy = None
        return JSONResponse(clean({
            "now_et": now.isoformat(timespec="seconds"),
            "session": session,
            "regime": regime,
            "spy": spy,
            "next_change_et": nxt.isoformat(timespec="seconds") if nxt else None,
            "replay": replay,
        }))

    @app.get("/api/v2/toplists")
    async def v2_toplists() -> JSONResponse:
        """Every list with its universe filter and default row count.

        The filter lives in the shared assignment map under `toplist:<name>`,
        not in the toplist settings file, so the Universe panel's "used by"
        count sees toplists the same way it sees setups.
        """
        assign = v2.profiles.assignments.load()
        rows = v2.toplists.settings.load()
        return JSONResponse(clean({"toplists": [{
            "name": n,
            "label": TOPLIST_LABEL.get(n, n),
            "universe": assign.get(assignment_key(n), ALL_ID),
            "rows": rows.get(n, {}).get("rows", 25),
            # "full" lists rank live states so every condition applies; the
            # premarket and HOD/LOD lists can only apply the static half.
            "scope": FILTER_SCOPE.get(n, "static"),
        } for n in FILTERABLE_LISTS]}))

    @app.put("/api/v2/toplists/{name}")
    async def v2_toplist_put(name: str, body: dict = Body(...)) -> JSONResponse:
        """Set one list's default row count and/or its universe filter."""
        if name not in FILTERABLE_LISTS:
            return JSONResponse({"error": f"unknown toplist: {name}"}, status_code=404)
        body = body if isinstance(body, dict) else {}
        try:
            if "rows" in body:
                v2.toplists.settings.save({name: {"rows": body["rows"]}})
            if "universe" in body:
                uni = body["universe"]
                v2.profiles.assignments.save(
                    {assignment_key(name): None if not uni or uni == ALL_ID else uni})
                v2.profiles.reload()
        except (ProfileError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return await v2_toplists()

    @app.get("/api/v2/toplists/{name}")
    async def v2_toplist(name: str, limit: int = Query(25, ge=1, le=200)) -> JSONResponse:
        if name not in TOPLISTS:
            return JSONResponse({"error": f"unknown toplist: {name}", "available": list(TOPLISTS)}, status_code=404)
        result = await asyncio.to_thread(v2.toplists.compute, name, limit)
        session, _ = session_now()
        return JSONResponse(clean({**result, "session": session}))

    @app.get("/api/v2/events")
    async def v2_events(since: int = Query(0, ge=0), limit: int = Query(200, ge=1, le=1000)) -> JSONResponse:
        """The HOD/LOD stream, scoped by the universe filter on `toplist:hod_lod`.

        Filtered on the way OUT rather than at the hook: the buffer stays whole,
        so changing the filter re-scopes the stream immediately and no other
        consumer of the buffer is affected by this window's choice.

        `seq` is always the true latest, never the last one that passed, so a
        client paging forward skips filtered events instead of re-fetching them.
        """
        latest, events = v2.events.since(since, limit)
        keep, uni = member_predicate(v2.profiles if v2.profiles_live else None, "hod_lod")
        if keep is not None:
            events = [e for e in events if keep(e.get("symbol", ""))]
        return JSONResponse(clean({"seq": latest, "events": events, "universe": uni}))

    @app.get("/api/v2/snapshot")
    async def v2_snapshot(symbols: str = Query("")) -> JSONResponse:
        want = [s.strip().upper() for s in symbols.split(",") if s.strip()]
        rows: dict[str, dict] = {}
        for sym in want:
            st = scanner._states.get(sym) if hasattr(scanner, "_states") else None
            if st is None:
                continue
            try:
                rows[sym] = state_to_snapshot(st)
            except Exception as exc:
                log.debug("snapshot %s: %s", sym, exc)
        return JSONResponse(clean({"as_of": datetime.now(_ET).isoformat(timespec="seconds"), "rows": rows}))

    @app.get("/api/v2/state/{symbol}")
    async def v2_state(symbol: str) -> JSONResponse:
        sym = symbol.upper()
        st = scanner._states.get(sym) if hasattr(scanner, "_states") else None
        if st is None:
            return JSONResponse({"found": False, "symbol": sym, "error": "not in universe"}, status_code=404)
        try:
            info = state_to_info(st, v2.universe_meta.get(sym), sector_of(sym))
        except Exception as exc:
            return JSONResponse({"found": False, "symbol": sym, "error": str(exc)}, status_code=500)
        return JSONResponse(clean(info))

    @app.get("/api/v2/fundamentals/{symbol}")
    async def v2_fundamentals(symbol: str) -> JSONResponse:
        sym = symbol.upper()
        entry = v2.fundamentals.get(sym)
        if entry is not None:
            return JSONResponse(clean(entry))
        v2.fundamentals.request(sym)
        return JSONResponse({"symbol": sym, "ok": False, "pending": True})

    @app.get("/api/v2/news")
    async def v2_news(
        symbols: str = Query(""),
        limit: int = Query(50, ge=1, le=50),
        hours: int = Query(24, ge=1, le=720),
    ) -> JSONResponse:
        syms = [s.strip().upper() for s in symbols.split(",") if s.strip()] or None
        result = await asyncio.to_thread(v2.news.fetch, syms, limit, hours)
        return JSONResponse(clean(result))

    @app.get("/api/v2/universe/meta")
    async def v2_universe_meta() -> JSONResponse:
        syms = sorted(scanner._states.keys()) if hasattr(scanner, "_states") else sorted(v2.universe_meta)
        meta = {s: v2.universe_meta.get(s, {}) for s in syms}
        for s in syms:
            if "sector_etf" not in meta[s] or meta[s].get("sector_etf") is None:
                meta[s] = {**meta[s], "sector_etf": sector_of(s)}
        return JSONResponse(clean({"symbols": syms, "meta": meta}))

    # ── settings (Config panel) ──────────────────────────────────────────────

    def settings_payload() -> dict:
        return {
            "schema": settings.schema(),
            "values": settings.values(),
            "defaults": settings.defaults(),
            "modified": settings.modified(),
            "hash": settings.hash,
            "presets": settings.list_presets(),
            "history": settings.history(50),
            "stats": gate_stats.snapshot(),
        }

    @app.get("/api/v2/settings")
    async def v2_settings_get() -> JSONResponse:
        return JSONResponse(clean(settings_payload()))

    @app.put("/api/v2/settings")
    async def v2_settings_put(body: dict = Body(...)) -> JSONResponse:
        try:
            changes = settings.save(body.get("values") or {}, source="ui", note=str(body.get("note") or ""))
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(clean({"ok": True, "changes": changes, **settings_payload()}))

    @app.post("/api/v2/settings/reset")
    async def v2_settings_reset(body: dict = Body(default={})) -> JSONResponse:
        keys = body.get("keys")
        setup = body.get("setup")
        changes = settings.reset(keys=keys if isinstance(keys, list) else None,
                                 setup=str(setup) if setup else None, note=str(body.get("note") or ""))
        return JSONResponse(clean({"ok": True, "changes": changes, **settings_payload()}))

    @app.get("/api/v2/settings/stats")
    async def v2_settings_stats() -> JSONResponse:
        return JSONResponse(clean(gate_stats.snapshot()))

    @app.put("/api/v2/settings/presets/{name}")
    async def v2_preset_save(name: str) -> JSONResponse:
        try:
            return JSONResponse(clean({"ok": True, "preset": settings.save_preset(name), "presets": settings.list_presets()}))
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/v2/settings/presets/{name}/apply")
    async def v2_preset_apply(name: str) -> JSONResponse:
        try:
            changes = settings.apply_preset(name)
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(clean({"ok": True, "changes": changes, **settings_payload()}))

    @app.delete("/api/v2/settings/presets/{name}")
    async def v2_preset_delete(name: str) -> JSONResponse:
        return JSONResponse({"ok": settings.delete_preset(name), "presets": settings.list_presets()})

    # ── layouts ──────────────────────────────────────────────────────────────

    @app.get("/api/v2/layouts")
    async def v2_layouts() -> JSONResponse:
        return JSONResponse({"screens": v2.layouts.load_all()})

    @app.put("/api/v2/layouts/{screen_id}")
    async def v2_layout_put(screen_id: str, screen: dict = Body(...)) -> JSONResponse:
        sid = sanitize_id(screen_id)
        if sid is None or screen.get("id") != sid:
            return JSONResponse({"error": "id mismatch or invalid id"}, status_code=400)
        if screen.get("version") != 1:
            return JSONResponse({"error": "unsupported screen version"}, status_code=400)
        layout = screen.get("layout")
        windows = screen.get("windows")
        if not isinstance(layout, list) or not isinstance(windows, dict):
            return JSONResponse({"error": "layout must be a list and windows a dict"}, status_code=400)
        ids = {str(item.get("i")) for item in layout if isinstance(item, dict)}
        if ids != set(windows.keys()):
            return JSONResponse({"error": "layout ids and window ids differ"}, status_code=400)
        try:
            v2.layouts.save(screen)
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "id": sid})

    @app.delete("/api/v2/layouts/{screen_id}")
    async def v2_layout_delete(screen_id: str) -> JSONResponse:
        return JSONResponse({"ok": v2.layouts.delete(screen_id)})


    # ── setups (system names + custom setups + trigger catalog) ─────────────

    def catalog_for_ui() -> list[dict]:
        return catalog_json()

    def setups_payload() -> dict:
        names = v2.setup_names.load()
        system = [{"code": s.code, "name": names[s.code], "default_name": s.name, "direction": s.direction}
                  for s in plugins.SYSTEM_SETUPS]
        custom = v2.setups.load_all()
        for c in custom:
            c["summary"] = summary_lines(c)
        ev = v2.custom_eval
        return {
            "system": system,
            "custom": custom,
            "catalog": catalog_for_ui(),
            "stats": ev.stats() if ev else {"since": None, "setups": {}},
            "live": ev is not None,
        }

    def _reload_eval() -> None:
        ev = v2.custom_eval
        if ev is not None:
            try:
                ev.reload()
            except Exception as exc:
                log.warning("custom evaluator reload failed: %s", exc)

    # ── universe profiles ────────────────────────────────────────────────────

    def profiles_payload() -> dict:
        eng = v2.profiles
        p = eng.payload()
        p["catalog"] = conditions_catalog_json()
        p["stats"] = profile_stats.snapshot()
        p["live"] = v2.profiles_live
        p["universe_size"] = len(getattr(scanner, "_states", {}) or {})
        return p

    @app.get("/api/v2/universe/profiles")
    async def v2_profiles() -> JSONResponse:
        return JSONResponse(clean(profiles_payload()))

    @app.put("/api/v2/universe/profiles/{profile_id}")
    async def v2_profile_put(profile_id: str, body: dict = Body(...)) -> JSONResponse:
        pid = sanitize_id(profile_id)
        if pid is None:
            return JSONResponse({"error": "invalid id"}, status_code=400)
        eng = v2.profiles
        try:
            saved = eng.store.save(body, pid=pid)
        except (ProfileError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        eng.reload()
        # Static conditions may have changed, so the session member sets are
        # stale. Re-resolve against the live states rather than dropping them,
        # otherwise every check falls back to evaluating inline until tomorrow.
        _resolve_members(eng)
        saved["summary"] = profile_summary_lines(saved)
        return JSONResponse(clean({"ok": True, "profile": saved, **profiles_payload()}))

    @app.delete("/api/v2/universe/profiles/{profile_id}")
    async def v2_profile_delete(profile_id: str) -> JSONResponse:
        eng = v2.profiles
        pid = sanitize_id(profile_id)
        if pid is None:
            return JSONResponse({"error": "invalid id"}, status_code=400)
        if pid == ALL_ID:
            return JSONResponse({"error": "the default profile cannot be deleted"},
                                status_code=400)
        # Refuse while something still points at it, rather than silently
        # unassigning setups the user is not looking at.
        users = [s["id"] for s in v2.setups.load_all() if s.get("universe_profile") == pid]
        users += [k for k, v in eng.assignments.load().items() if v == pid]
        if users:
            return JSONResponse(
                {"error": f"still in use by: {', '.join(sorted(users))}"}, status_code=409)
        ok = eng.store.delete(pid)
        eng.reload()
        return JSONResponse(clean({"ok": ok, **profiles_payload()}))

    @app.put("/api/v2/universe/paramsets/{set_id}")
    async def v2_paramset_put(set_id: str, body: dict = Body(...)) -> JSONResponse:
        eng = v2.profiles
        try:
            eng.param_sets.save(dict(body or {}, id=set_id), pid=sanitize_id(set_id) or set_id)
        except (ProfileError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        eng.reload()
        return JSONResponse(clean({"ok": True, **profiles_payload()}))

    @app.delete("/api/v2/universe/paramsets/{set_id}")
    async def v2_paramset_delete(set_id: str) -> JSONResponse:
        eng = v2.profiles
        used = [s["id"] for s in v2.setups.load_all() if s.get("parameter_set") == set_id]
        if used:
            return JSONResponse(
                {"error": f"still used by {', '.join(used)}"}, status_code=409)
        if not eng.param_sets.delete(set_id):
            return JSONResponse({"error": "unknown parameter set"}, status_code=404)
        eng.reload()
        return JSONResponse(clean({"ok": True, **profiles_payload()}))

    @app.put("/api/v2/universe/assignments")
    async def v2_profile_assign(body: dict = Body(...)) -> JSONResponse:
        eng = v2.profiles
        try:
            eng.assignments.save(body if isinstance(body, dict) else {})
        except (ProfileError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        eng.reload()
        _resolve_members(eng)
        return JSONResponse(clean({"ok": True, **profiles_payload()}))

    @app.get("/api/v2/universe/profiles/{profile_id}/members")
    async def v2_profile_members(profile_id: str,
                                 limit: int = Query(200)) -> JSONResponse:
        """How many symbols a profile admits, before you attach it to anything.

        Only the STATIC half can be answered here; the dynamic conditions are a
        function of the current bar and are reported separately so the count is
        not mistaken for the whole screen.
        """
        eng = v2.profiles
        cp = eng.get(sanitize_id(profile_id) or "")
        if cp is None:
            return JSONResponse({"error": "unknown profile"}, status_code=404)
        states = getattr(scanner, "_states", {}) or {}
        if cp.members is None and cp.static:
            _resolve_members(eng)
        members = sorted(cp.members) if cp.members is not None else sorted(states)
        return JSONResponse(clean({
            "id": cp.id, "name": cp.name,
            "universe_size": len(states),
            "count": len(members),
            "symbols": members[:max(0, limit)],
            "truncated": len(members) > max(0, limit),
            "static_conditions": len(cp.static),
            "dynamic_conditions": len(cp.dynamic),
        }))

    def _resolve_members(eng: ProfileEngine) -> None:
        states = getattr(scanner, "_states", {}) or {}
        if not states:
            return
        try:
            # FundamentalsCache exposes .get(symbol), which is the whole
            # interface resolve_members needs; no need to materialise a dict.
            eng.resolve_members(states, get_fundamentals_cache())
        except Exception as exc:
            log.warning("member resolution failed: %s", exc)

    @app.get("/api/v2/setups")
    async def v2_setups() -> JSONResponse:
        return JSONResponse(clean(setups_payload()))

    @app.get("/api/v2/capabilities")
    async def v2_capabilities() -> JSONResponse:
        """Which optional features this build has (system setups, TOS bridge)."""
        return JSONResponse(plugins.capabilities())

    @app.get("/api/v2/setups/catalog")
    async def v2_setups_catalog() -> JSONResponse:
        return JSONResponse({"catalog": catalog_for_ui()})

    @app.put("/api/v2/setups/names")
    async def v2_setups_names(body: dict = Body(...)) -> JSONResponse:
        names = v2.setup_names.save(body if isinstance(body, dict) else {})
        return JSONResponse({"ok": True, "names": names})

    @app.put("/api/v2/setups/{setup_id}")
    async def v2_setup_put(setup_id: str, body: dict = Body(...)) -> JSONResponse:
        sid = sanitize_id(setup_id)
        if sid is None:
            return JSONResponse({"error": "invalid id"}, status_code=400)
        try:
            saved = v2.setups.save(body, sid=sid)
        except SetupError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)
        _reload_eval()
        saved["summary"] = summary_lines(saved)
        return JSONResponse(clean({"ok": True, "setup": saved}))

    @app.delete("/api/v2/setups/{setup_id}")
    async def v2_setup_delete(setup_id: str) -> JSONResponse:
        ok = v2.setups.delete(setup_id)
        _reload_eval()
        return JSONResponse({"ok": ok})

    @app.get("/api/v2/setups/{setup_id}/check")
    async def v2_setup_check(setup_id: str, symbol: str = Query("")) -> JSONResponse:
        sym = symbol.strip().upper()
        if not sym:
            return JSONResponse({"error": "symbol required"}, status_code=400)
        try:
            state = scanner._states.get(sym)
        except Exception:
            state = None
        if state is None:
            return JSONResponse(clean({"symbol": sym, "in_universe": False, "triggers": [], "gates": [],
                                       "message": f"{sym} is not in the scanner universe (data/universe.csv)."}))
        code = setup_id.upper()
        if code in plugins.SYSTEM_CODES:
            spy = getattr(scanner, "_spy_state", None)
            return JSONResponse(clean(await asyncio.to_thread(plugins.system_check, code, state, spy)))
        setup = v2.setups.get(setup_id)
        if setup is None:
            return JSONResponse({"error": "unknown setup"}, status_code=404)
        ev = v2.custom_eval
        if ev is None:
            return JSONResponse(clean({"symbol": sym, "in_universe": True, "triggers": [],
                                       "message": "custom evaluator not attached (scanner started without it)"}))
        return JSONResponse(clean(await asyncio.to_thread(ev.check, setup, state)))

    @app.get("/api/v2/check/{symbol}")
    async def v2_check_all(symbol: str, minutes: int = Query(5, ge=1, le=15)) -> JSONResponse:
        """Setup check window: every setup, what it did on this stock in the last
        few minutes, and why (see scanner/recent_activity.py)."""
        def build() -> dict:
            try:
                custom = v2.setups.load_all()
            except Exception:
                custom = []
            try:
                names = v2.setup_names.load()
            except Exception:
                names = {}
            return build_setup_check(scanner, custom, names, getattr(v2, "profiles", None),
                                     symbol, minutes)
        return JSONResponse(clean(await asyncio.to_thread(build)))

    # ── candidate screener / selected universe ───────────────────────────────

    def universe_selection_payload() -> dict:
        selected_id = v2.universe_selection.load()
        watchlists = v2.watchlists.load_all()
        selected = next((w for w in watchlists if w.get("id") == selected_id), None)
        states = getattr(scanner, "_states", {}) or {}
        sector_symbols = set(getattr(scanner, "_sector_symbols", ()) or ())
        if not sector_symbols:
            sector_symbols = set((getattr(scanner, "_sector_map", {}) or {}).values())
        sector_symbols.discard("SPY")
        current_streams = stream_symbols(sorted(states), sorted(sector_symbols))
        selected_symbols = list((selected or {}).get("symbols") or [])
        selected_streams = stream_symbols(selected_symbols, sorted(sector_symbols)) if selected else []
        return {
            "watchlist_id": selected_id,
            "watchlist_name": (selected or {}).get("name") if selected else None,
            "watchlist_found": selected is not None or selected_id is None,
            "selected_count": len(selected_symbols),
            "selected_total": len(selected_streams),
            "current_count": len(states),
            "current_total": len(current_streams),
            "support_count": max(0, len(current_streams) - len(states)),
            "support_symbols": [s for s in current_streams if s not in states],
            "cap": _SCHWAB_CHART_CAP,
            "safe_watchlist_cap": _SAFE_WATCHLIST_CAP,
            "applies_on_restart": True,
            "applied": bool(selected and set(selected_symbols) == set(states)),
        }

    @app.get("/api/v2/screener/yahoo/catalog")
    async def v2_yahoo_screener_catalog() -> JSONResponse:
        return JSONResponse({"presets": [{"id": key, "label": label}
                                          for key, label in YAHOO_PRESETS.items()]})

    @app.post("/api/v2/screener/yahoo")
    async def v2_yahoo_screener(body: dict = Body(...)) -> JSONResponse:
        try:
            result = await asyncio.to_thread(v2.yahoo_screener.run, body if isinstance(body, dict) else {})
        except YahooScreenerError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse(clean(result))

    @app.get("/api/v2/universe/selection")
    async def v2_universe_selection() -> JSONResponse:
        return JSONResponse(clean(universe_selection_payload()))

    @app.put("/api/v2/universe/selection")
    async def v2_universe_selection_put(body: dict = Body(...)) -> JSONResponse:
        wid = body.get("watchlist_id") if isinstance(body, dict) else None
        if wid in (None, ""):
            v2.universe_selection.save(None)
            return JSONResponse(clean({"ok": True, **universe_selection_payload()}))
        sid = sanitize_id(wid)
        if sid is None:
            return JSONResponse({"error": "invalid watchlist id"}, status_code=400)
        watchlist = next((w for w in v2.watchlists.load_all() if w.get("id") == sid), None)
        if watchlist is None:
            return JSONResponse({"error": "watchlist not found"}, status_code=404)
        count = len(watchlist.get("symbols") or [])
        if count == 0:
            return JSONResponse({"error": "an empty watchlist cannot be the scanner universe"}, status_code=400)
        if count > _SAFE_WATCHLIST_CAP:
            return JSONResponse({
                "error": f"watchlist has {count} symbols; the safe maximum is {_SAFE_WATCHLIST_CAP} "
                         f"because {_SUPPORT_RESERVE} Schwab streams are reserved for SPY and sector ETFs"
            }, status_code=400)
        v2.universe_selection.save(sid)
        return JSONResponse(clean({"ok": True, **universe_selection_payload()}))

    # ── watchlists ───────────────────────────────────────────────────────────

    @app.get("/api/v2/watchlists")
    async def v2_watchlists() -> JSONResponse:
        return JSONResponse({"watchlists": v2.watchlists.load_all()})

    @app.put("/api/v2/watchlists/{wl_id}")
    async def v2_watchlist_put(wl_id: str, wl: dict = Body(...)) -> JSONResponse:
        wid = sanitize_id(wl_id)
        if wid is None or wl.get("id") != wid:
            return JSONResponse({"error": "id mismatch or invalid id"}, status_code=400)
        if v2.universe_selection.load() == wid and len(wl.get("symbols") or []) > _SAFE_WATCHLIST_CAP:
            return JSONResponse({
                "error": f"this is the selected scanner universe; it cannot exceed "
                         f"{_SAFE_WATCHLIST_CAP} watchlist symbols"
            }, status_code=400)
        try:
            rec = v2.watchlists.save(wl)
        except (ValueError, OSError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True, "watchlist": rec})

    @app.delete("/api/v2/watchlists/{wl_id}")
    async def v2_watchlist_delete(wl_id: str) -> JSONResponse:
        ok = v2.watchlists.delete(wl_id)
        if ok and v2.universe_selection.load() == sanitize_id(wl_id):
            v2.universe_selection.save(None)
        return JSONResponse({"ok": ok})

    return v2


_missing_logged = False


def mount_v2_static(app: FastAPI, dist: Path = _V2_DIST) -> None:
    """Serve dashboard-v2/dist at /v2 with an SPA fallback. Plain routes (not a
    mount) so they win over the old dashboard's "/" mount registered after."""
    index = dist / "index.html"

    def _serve(path: str = ""):
        global _missing_logged
        if not index.exists():
            if not _missing_logged:
                log.warning("api_v2: %s not built; run `npm --prefix dashboard-v2 run build`", dist)
                _missing_logged = True
            return JSONResponse({"error": "dashboard-v2 not built"}, status_code=503)
        target = (dist / path).resolve() if path else index
        try:
            inside = target.is_relative_to(dist.resolve())
        except AttributeError:      # py<3.9 (not our case) — be safe anyway
            inside = str(target).startswith(str(dist.resolve()))
        if path and inside and target.is_file():
            # Windows commonly registers .js as text/plain. Browsers refuse to
            # execute an ES module served with that MIME type, leaving V2 blank.
            media_type = "text/javascript" if target.suffix.lower() in (".js", ".mjs") else None
            return FileResponse(target, media_type=media_type)
        return FileResponse(index)

    @app.get("/v2")
    async def v2_root():
        return _serve("")

    @app.get("/v2/{path:path}")
    async def v2_any(path: str):
        return _serve(path)
