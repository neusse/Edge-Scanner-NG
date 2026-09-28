"""Toplists for Dashboard V2, computed from the live SymbolState map.

Pure `build_rows(states, name, limit)` does the ranking on a list of
(symbol, state) pairs the caller has already copied out of the scanner (one
`list(scanner._states.items())` per request, so the scanner thread can keep
mutating the dict underneath). `ToplistEngine` wraps it with a 2 s cache so
several dashboard windows polling the same list cost one computation.

Pre-market lists (pm_gainers / pm_losers / pm_volume) are NOT here: the
frontend reads the existing `/api/premarket` for those.

Each list can carry a universe profile, assigned in the Config panel and stored
under the key `toplist:<name>` in the same `data/setups/profiles.json` every
setup uses. The ranking then runs over the symbols that profile admits instead
of the whole universe, which is what makes a 6,455-symbol base list usable: one
`rvol` list scoped to Liquid movers and another scoped to Small cap runners are
two genuinely different screens off the same metric.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from scanner.conditions import ConditionCtx
from scanner.json_store import _read_json, _write_json_atomic

log = logging.getLogger(__name__)

_ET = ZoneInfo("America/New_York")

TOPLISTS: tuple[str, ...] = (
    "rvol", "gainers_close", "losers_close", "gainers_open", "losers_open", "movers_5m",
)

# The other lists the dashboard offers. They are NOT ranked here: the premarket
# three are built in `api.get_premarket` from 04:00-09:29 bars, and hod_lod is a
# STREAM off `EventBuffer` with no metric and no sort. They still take a universe
# filter, so they are assignable on the same mechanism and appear in the same
# Config list; only the place the filter is applied differs.
PREMARKET_LISTS: tuple[str, ...] = ("pm_gainers", "pm_losers", "pm_volume")
STREAM_LISTS: tuple[str, ...] = ("hod_lod",)
FILTERABLE_LISTS: tuple[str, ...] = TOPLISTS + PREMARKET_LISTS + STREAM_LISTS

TOPLIST_LABEL: dict[str, str] = {
    "rvol": "RVOL leaders",
    "gainers_close": "Gainers (from close)",
    "losers_close": "Losers (from close)",
    "gainers_open": "Gainers (from open)",
    "losers_open": "Losers (from open)",
    "movers_5m": "5-min movers",
    "pm_gainers": "Pre-market gainers",
    "pm_losers": "Pre-market losers",
    "pm_volume": "Pre-market volume",
    "hod_lod": "New HOD / LOD",
}

# How much of a profile a list can actually apply.
#   full    every condition, static and dynamic, against the current bar
#   static  membership only: there is no current bar to evaluate the dynamic
#           half against. A premarket list is built from 04:00-09:29 bars and a
#           HOD/LOD event is historical by the time it is read, so asking
#           "was rvol above 2 just now" of either one is meaningless.
FILTER_SCOPE: dict[str, str] = {
    **{n: "full" for n in TOPLISTS},
    **{n: "static" for n in PREMARKET_LISTS + STREAM_LISTS},
}

_DEFAULT_ROWS = 25
_SETTINGS_FILE = Path("data/setups/toplists.json")


def assignment_key(name: str) -> str:
    """The `SetupProfiles` key holding this list's universe filter."""
    return f"toplist:{name}"


def member_predicate(engine, list_name: str):
    """(predicate on SYMBOL, meta) for a list that cannot evaluate a full profile.

    Static half only, per FILTER_SCOPE. Fails OPEN when the member set has not
    been resolved yet, which is the opposite of the alert path: an unresolved
    profile there must not let an unvetted symbol fire, but here it would blank
    a display list and look like a broken scanner. Nothing downstream trades
    off these lists.
    """
    meta = {"id": None, "name": None, "hash": None, "scope": FILTER_SCOPE.get(list_name, "static")}
    if engine is None:
        return None, meta
    cp = engine.for_setup(assignment_key(list_name))
    if cp is None or cp.is_empty:
        meta.update(id=getattr(cp, "id", None), name=getattr(cp, "name", None),
                    hash=getattr(cp, "hash", None))
        return None, meta
    meta.update(id=cp.id, name=cp.name, hash=cp.hash)
    if not cp.static:
        # Nothing but dynamic conditions, and this list cannot evaluate those.
        meta["scope"] = "none"
        return None, meta
    members = cp.members
    if members is None:
        log.info("profile %s has no member set yet; %s is unfiltered this cycle",
                 cp.id, list_name)
        return None, meta
    return (lambda sym: sym in members), meta


class ToplistSettings:
    """Per-list display defaults. The universe filter is NOT here: it lives in
    `data/setups/profiles.json` with every other assignment, so one list shows
    everything a profile is used by."""

    def __init__(self, path: Path = _SETTINGS_FILE) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, dict]:
        d = _read_json(self._path) if self._path.exists() else None
        out = {n: {"rows": _DEFAULT_ROWS} for n in FILTERABLE_LISTS}
        if isinstance(d, dict):
            for name, cfg in d.items():
                if name in out and isinstance(cfg, dict):
                    try:
                        out[name]["rows"] = max(1, min(200, int(cfg.get("rows", _DEFAULT_ROWS))))
                    except (TypeError, ValueError):
                        pass
        return out

    def save(self, mapping: dict) -> dict[str, dict]:
        cur = self.load()
        for name, cfg in (mapping or {}).items():
            if name not in cur:
                raise ValueError(f"unknown toplist: {name!r}")
            if not isinstance(cfg, dict):
                raise ValueError(f"{name}: expected an object")
            if "rows" in cfg:
                try:
                    cur[name]["rows"] = max(1, min(200, int(cfg["rows"])))
                except (TypeError, ValueError):
                    raise ValueError(f"{name}: rows must be a number")
        with self._lock:
            _write_json_atomic(self._path, cur)
        return cur


def _price_of(state) -> Optional[float]:
    # Lazy import: api_v2 owns the single `_last_close` read (and imports this module).
    from scanner.api_v2 import price_of
    return price_of(state)


def _finite(v: Optional[float]) -> Optional[float]:
    """None for anything that is not a real number, so NaN never reaches JSON."""
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pct(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return (a - b) / b * 100.0


def _metric(st, name: str, price: Optional[float]) -> Optional[float]:
    """The list's ranking value for one state (None -> row skipped)."""
    if name == "rvol":
        return st.rvol
    if name in ("gainers_close", "losers_close"):
        return _pct(price, st.prior_close)
    if name in ("gainers_open", "losers_open"):
        return st.rth_chg_pct
    if name == "movers_5m":
        bars = list(st.last_1m)
        if len(bars) >= 6:
            ref = bars[-6]["close"]
            return _pct(bars[-1]["close"], ref)
        return st.mom_15m_pct
    raise ValueError(f"unknown toplist: {name}")


def build_rows(
    states: list[tuple[str, object]],
    name: str,
    limit: int = 25,
    *,
    price_fn: Callable[[object], Optional[float]] = _price_of,
    keep: Optional[Callable[[str, object], bool]] = None,
) -> list[dict]:
    """Rank `states` for toplist `name`. Rows whose price/value is None are skipped.

    Row shape: {symbol, price, value, chg_pct, rvol, volume}. `value` is the
    list's own metric (signed for movers_5m, which sorts by magnitude);
    `chg_pct` is always vs prior close.

    `keep` is the universe filter: a symbol it rejects never reaches the
    ranking. It is applied BEFORE the metric is computed, so a filter that
    removes most of the universe makes the list cheaper, not dearer.
    """
    if name not in TOPLISTS:
        raise ValueError(f"unknown toplist: {name}")
    rows: list[dict] = []
    for sym, st in states:
        try:
            if keep is not None and not keep(sym, st):
                continue
            price = price_fn(st)
            if price is None or not math.isfinite(price):
                continue
            value = _metric(st, name, price)
            # NaN, not just None: `rvol` is NaN for a symbol with no volume
            # profile, and NaN survives an `is None` check, ranks unpredictably
            # against real values and serialises as null. Harmless on a
            # few-hundred-symbol universe, dominant on a wide one.
            if value is None or not math.isfinite(value):
                continue
            rows.append({
                "symbol": sym,
                "price": float(price),
                "value": float(value),
                "chg_pct": _finite(_pct(price, st.prior_close)),
                "rvol": _finite(st.rvol),
                "volume": getattr(st, "_cum_vol", None),
            })
        except Exception as exc:  # torn read from the scanner thread: drop the row
            log.debug("toplist %s: skipping %s: %s", name, sym, exc)
            continue

    if name == "movers_5m":
        rows.sort(key=lambda r: abs(r["value"]), reverse=True)
    elif name in ("losers_close", "losers_open"):
        rows.sort(key=lambda r: r["value"])
    else:
        rows.sort(key=lambda r: r["value"], reverse=True)
    return rows[: max(1, int(limit))]


class ToplistEngine:
    """Per-list cache over `build_rows` fed from the live scanner.

    The universe filter is resolved per request from the scanner's own
    ProfileEngine, so saving a filter in the Config panel changes the list on
    the next poll with no restart. The profile hash is part of the cache key,
    which is what makes that true.
    """

    def __init__(self, scanner, ttl: float = 2.0,
                 settings: Optional[ToplistSettings] = None) -> None:
        self._scanner = scanner
        self._ttl = ttl
        self._cache: dict[str, tuple[float, int, dict]] = {}
        self._lock = threading.Lock()
        self.settings = settings or ToplistSettings()

    # -- universe filter --

    def _engine(self):
        from scanner.profiles import ProfileEngine
        eng = getattr(self._scanner, "_profiles", None)
        return eng if isinstance(eng, ProfileEngine) else None

    def _filter_for(self, name: str) -> tuple[Optional[Callable[[str, Any], bool]], dict]:
        """(predicate, meta) for one list. Predicate is None when unfiltered."""
        eng = self._engine()
        if eng is None:
            return None, {"id": None, "name": None, "hash": None}
        cp = eng.for_setup(assignment_key(name))
        if cp is None or cp.is_empty:
            return None, {"id": getattr(cp, "id", None), "name": getattr(cp, "name", None),
                          "hash": getattr(cp, "hash", None)}
        series = getattr(self._scanner, "_series", {}) or {}
        funds = _fundamentals()   # FundamentalsCache or None

        def keep(sym: str, st: Any) -> bool:
            # Static conditions collapse to a member-set lookup, so this is one
            # hash probe for most symbols and only the survivors pay for the
            # dynamic half. No BarProfileCache: rows are one symbol each, so
            # there is nothing to share between them.
            ctx = ConditionCtx(state=st, series=series.get(sym), bar=None,
                               fundamentals=funds.get(sym) if funds is not None else None)
            try:
                return eng.check(cp, ctx).passed
            except Exception:
                return False

        return keep, {"id": cp.id, "name": cp.name, "hash": cp.hash}

    def compute(self, name: str, limit: int = 25) -> dict:
        if name not in TOPLISTS:
            raise ValueError(f"unknown toplist: {name}")
        limit = max(1, int(limit))
        profile_keep, uni = self._filter_for(name)
        dynamic_ready = getattr(self._scanner, "dynamic_ranking_enabled", None)

        def keep(symbol: str, state: Any) -> bool:
            if callable(dynamic_ready) and not dynamic_ready(symbol):
                return False
            return profile_keep(symbol, state) if profile_keep is not None else True

        key = f"{name}|{uni.get('hash') or '-'}"
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
            if hit and (now - hit[0]) < self._ttl and hit[1] >= limit:
                cached = hit[2]
                return {**cached, "rows": cached["rows"][:limit]}

        states = list(self._scanner._states.items())
        rows = build_rows(states, name, limit, keep=keep)
        result = {
            "list": name,
            "as_of": datetime.now(_ET).isoformat(timespec="seconds"),
            "universe": uni,
            "scanned": len(states),
            "rows": rows,
        }
        with self._lock:
            self._cache[key] = (now, limit, result)
        return result


def _fundamentals():
    """The process-wide fundamentals cache, or None when it cannot be built."""
    try:
        from scanner.fundamentals import get_cache
        return get_cache()
    except Exception:
        return None
