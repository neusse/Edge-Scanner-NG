"""Custom setups: user-composed "universe + triggers" alert strategies.

  * CustomSetupStore  -- data/setups/custom/<id>.json, seeded from
                         scanner/custom_setups_defaults.json on first run.
  * SetupNames        -- data/setups/names.json: display names for the system
                         setups an installed engine plugin provides (their wire
                         codes never change).
  * CustomEvaluator   -- runs on every bar inside LiveScanner._on_bar(), AFTER any
                         system-setup evaluators, and emits alerts through its
                         sink with setup=<custom id> and custom=true, so the
                         dashboard and downstream clients see them on the unified
                         alert feed they already read.

Definition (JSON):
  {
    "id": "cs_up_volume", "name": "Up Volume", "color": "#4caf50", "enabled": true,
    "mode": "or" | "and",            # any trigger fires / all fire within and_window_min
    "direction": "all" | "long" | "short",
    "alert_direction": "" | "long" | "short" | "neutral",  # optional display/feed override
    "sessions": ["rth"] | ["pre", "rth"],
    "repeat_sec": 0,                 # do not repeat the same symbol for N seconds
    "and_window_min": 5,
    "size_hint": "half",
    "triggers": [{"id": "new_candle_high", "options": ["5"], "params": {"since": 1}, "repeat_sec": 0}],
    "notes": "", "pending_filters": [...], "source": "user" | "sample"
  }
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

from scanner.conditions import (
    CATALOG, ConditionError, describe as describe_condition, normalize_condition,
)
from scanner.json_store import _read_json, _write_json_atomic, sanitize_id
from scanner.trigger_catalog import (
    BY_ID, EvalCtx, Fire, SymbolSeries, describe, et_minutes, evaluate, n_day_latch_key,
    n_day_level, trigger_key,
)

log = logging.getLogger(__name__)

from scanner import plugins  # noqa: E402

_DEFAULTS_FILE = Path(__file__).parent / "custom_setups_defaults.json"
_SETUPS_DIR = Path("data/setups")
# System setup codes and default names come from the installed plugin, if any.
_SYSTEM_CODES = plugins.SYSTEM_CODES
_SYSTEM_DEFAULT_NAMES = plugins.SYSTEM_DEFAULT_NAMES
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,48}$")
_MODES = ("or", "and", "atleast")
_DIRS = ("all", "long", "short")
_ALERT_DIRS = ("", "long", "short", "neutral")
_SESSIONS = ("pre", "rth")
_SIZES = ("full", "three_quarter", "half")


# ── validation ───────────────────────────────────────────────────────────────

class SetupError(ValueError):
    pass


def normalize_setup(raw: dict, *, existing_id: Optional[str] = None) -> dict:
    """Validate + normalise a custom setup definition. Raises SetupError."""
    try:
        return _normalize_setup(raw, existing_id=existing_id)
    except (ValueError, TypeError, OverflowError) as exc:
        # "abc", NaN, infinity or a list where a number belongs. The API maps
        # SetupError to a 400; anything else would surface as a 500.
        raise SetupError(f"a numeric field has an invalid value ({exc})") from exc


def _normalize_setup(raw: dict, *, existing_id: Optional[str] = None) -> dict:
    if not isinstance(raw, dict):
        raise SetupError("setup must be an object")
    sid = str(raw.get("id") or existing_id or "").strip()
    if not _ID_RE.match(sid):
        raise SetupError("invalid setup id")
    if sid in _SYSTEM_CODES:
        raise SetupError("id collides with a system setup code")
    name = str(raw.get("name") or "").strip()[:60]
    if not name:
        raise SetupError("name is required")
    mode = str(raw.get("mode") or "or")
    if mode not in _MODES:
        raise SetupError("mode must be or / and / atleast")
    direction = str(raw.get("direction") or "all")
    if direction not in _DIRS:
        raise SetupError("direction must be all / long / short")
    alert_direction = str(raw.get("alert_direction") or "")
    if alert_direction not in _ALERT_DIRS:
        raise SetupError("alert_direction must be blank / long / short / neutral")
    sessions = [s for s in (raw.get("sessions") or ["rth"]) if s in _SESSIONS] or ["rth"]
    size_hint = str(raw.get("size_hint") or "half")
    if size_hint not in _SIZES:
        size_hint = "half"
    trig_out: list[dict] = []
    for t in raw.get("triggers") or []:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "")
        tdef = BY_ID.get(tid)
        if tdef is None:
            raise SetupError(f"unknown trigger {tid!r}")
        opts_raw = t.get("options")
        valid = {o.key for o in tdef.options}
        if tdef.options:
            opts = [str(o) for o in (opts_raw or []) if str(o) in valid]
            if not opts:
                opts = list(tdef.default_options) or [tdef.options[0].key]
        else:
            opts = []
        params: dict[str, float] = {}
        raw_params = t.get("params") or {}
        if tid in ("vwap_support", "vwap_resistance") and "tol_pct" in raw_params and "tol_unit" not in raw_params:
            # saved before tol_unit existed, when the tolerance was always % of
            # price; the new default unit (% of daily ATR) must not reinterpret it
            t = dict(t, params=dict(raw_params, tol_unit=0))
        for pdef in tdef.params:
            v = (t.get("params") or {}).get(pdef.key, pdef.default)
            try:
                v = float(v)
            except Exception:
                raise SetupError(f"{tid}.{pdef.key} must be a number")
            if pdef.min is not None:
                v = max(pdef.min, v)
            if pdef.max is not None:
                v = min(pdef.max, v)
            if pdef.choices:
                v = min(pdef.choices, key=lambda ch: abs(ch - v))    # snap to an allowed value
            params[pdef.key] = v
        rep = int(float(t.get("repeat_sec") or 0))
        trig_out.append({"id": tid, "options": opts, "params": params, "repeat_sec": max(0, rep)})
    params_out: list[dict] = []
    seen_p: set[tuple[str, str]] = set()
    for c in (raw.get("parameters") or []):
        try:
            nc = normalize_condition(c)
        except ConditionError as exc:
            raise SetupError(str(exc))
        d = CATALOG.get(nc["id"])
        if d is not None and d.kind != "dynamic":
            raise SetupError(
                f"{d.name} is a universe condition, not a parameter: it cannot change "
                "during the session. Put it in a universe filter instead.")
        key = (nc["id"], nc.get("option", ""))
        if key in seen_p:
            raise SetupError(
                f"{nc['id']} appears twice with the same option; parameters are ANDed, "
                "so use one row with the tighter value")
        seen_p.add(key)
        params_out.append(nc)
    if len(params_out) > 20:
        raise SetupError("a setup is limited to 20 parameters")

    color = str(raw.get("color") or "#3b82f6")
    if not re.match(r"^#[0-9a-fA-F]{6}$", color):
        color = "#3b82f6"
    now = pd.Timestamp.utcnow().isoformat()
    return {
        "id": sid,
        "name": name,
        "color": color,
        "enabled": bool(raw.get("enabled", True)),
        "mode": mode,
        "direction": direction,
        "alert_direction": alert_direction,
        "sessions": sessions,
        "repeat_sec": max(0, int(float(raw.get("repeat_sec") or 0))),
        "and_window_min": max(1, int(float(raw.get("and_window_min") or 5))),
        # How many of the selected alerts must fire, for mode "atleast", e.g.
        # "2 of these 3 alerts fired inside the window", which neither AND nor
        # OR can express. AND is the same mechanism with the count pinned to all
        # of them, so this shares its rolling window.
        "min_triggers": max(1, int(float(raw.get("min_triggers") or 2))),
        "size_hint": size_hint,
        "triggers": trig_out,
        "notes": str(raw.get("notes") or "")[:2000],
        # Universe profile this setup is screened against (scanner/profiles.py).
        # Empty means no profile: every symbol in the base universe is eligible.
        # Not validated against the store here on purpose, so a setup survives
        # its profile being deleted; ProfileEngine.for_setup returns None for an
        # unknown id and an unknown profile passes rather than blocking.
        "universe_profile": (str(raw.get("universe_profile")).strip()
                             if raw.get("universe_profile") else ""),
        # Per-setup dynamic conditions: "what is the stock doing right now".
        # The split is deliberate: a UNIVERSE filter holds only session-fixed
        # instrument properties (price, float, ADV, ATR%, market cap) and is
        # shared by name, while PARAMETERS hold the bar-to-bar behaviour (RVOL,
        # distance from VWAP, % change) and belong to one setup. Mixing them
        # would turn a universe into something with no universe content in it,
        # and cost the member-set optimisation that only static conditions
        # can use.
        "parameters": params_out,
        # A shared, named list of dynamic conditions (scanner/profiles.py
        # ParamSetStore). ANDed with `parameters` above, so a setup can tighten
        # a shared value but never loosen it, which is the same one-way property
        # a universe filter has. Not validated against the store here, matching
        # universe_profile: an unknown id passes rather than blocking, so
        # deleting a set cannot silence every setup that referenced it.
        "parameter_set": (str(raw.get("parameter_set")).strip()
                          if raw.get("parameter_set") else ""),
        "pending_filters": [str(x) for x in (raw.get("pending_filters") or [])][:40],
        "source": str(raw.get("source") or "user"),
        "createdAt": str(raw.get("createdAt") or now),
        "updatedAt": now,
    }


def summary_lines(setup: dict) -> dict:
    """Plain-English summary of a setup (shown on the setup's Summary tab)."""
    trig = [describe(t["id"], o, t.get("params") or {}) for t in setup.get("triggers", []) for o in (t.get("options") or [""])]
    return {
        "universe": "Only symbols in the scanner universe (data/universe.csv, built from the liquidity screen) are scanned.",
        "mode": ("Any one of the alerts below fires the setup." if setup.get("mode") == "or"
                 else f"At least {setup.get('min_triggers', 2)} of the alerts below must fire "
                      f"within {setup.get('and_window_min', 5)} minutes."
                 if setup.get("mode") == "atleast"
                 else f"All alerts below must fire within {setup.get('and_window_min', 5)} minutes."),
        "direction": (
            {"all": "Long and short alerts.", "long": "Long alerts only.", "short": "Short alerts only."}[
                setup.get("direction", "all")]
            + (f" Reported as {setup['alert_direction']}." if setup.get("alert_direction") else "")
        ),
        "sessions": "Premarket and regular session." if "pre" in setup.get("sessions", []) else "Regular session only (09:30-16:00 ET).",
        "repeat": (f"The same symbol does not repeat for {setup['repeat_sec']} seconds." if setup.get("repeat_sec") else
                   "The same symbol + alert does not repeat within the feed's 5-minute cooldown."),
        "alerts": trig,
        "parameter_set": setup.get("parameter_set") or "",
        "parameters": [describe_condition(c) for c in setup.get("parameters", [])],
        "pending_filters": setup.get("pending_filters") or [],
    }


# ── stores ───────────────────────────────────────────────────────────────────

class CustomSetupStore:
    """One JSON file per custom setup; seeded from the defaults file when empty."""

    def __init__(self, dir: Path = _SETUPS_DIR / "custom", defaults: Path = _DEFAULTS_FILE) -> None:
        self._dir = Path(dir)
        self._defaults = defaults
        self._lock = threading.Lock()
        self._dir.mkdir(parents=True, exist_ok=True)
        self._seed_if_empty()

    @property
    def dir(self) -> Path:
        return self._dir

    def _seed_if_empty(self) -> None:
        try:
            if any(self._dir.glob("*.json")):
                return
            data = _read_json(self._defaults) if self._defaults.exists() else None
            for raw in (data or {}).get("setups", []):
                try:
                    s = normalize_setup(raw)
                    _write_json_atomic(self._dir / f"{s['id']}.json", s)
                except Exception as exc:
                    log.warning("seed setup skipped: %s", exc)
        except Exception as exc:
            log.warning("custom setup seeding failed: %s", exc)

    def load_all(self) -> list[dict]:
        out: list[dict] = []
        with self._lock:
            for p in sorted(self._dir.glob("*.json")):
                d = _read_json(p)
                if isinstance(d, dict) and d.get("id"):
                    out.append(d)
        out.sort(key=lambda s: (s.get("createdAt") or "", s.get("name") or ""))
        return out

    def get(self, sid: str) -> Optional[dict]:
        sid2 = sanitize_id(sid)
        if not sid2:
            return None
        d = _read_json(self._dir / f"{sid2}.json")
        return d if isinstance(d, dict) else None

    def save(self, raw: dict, *, sid: Optional[str] = None) -> dict:
        s = normalize_setup(raw, existing_id=sid)
        if sid and s["id"] != sid:
            raise SetupError("id in body does not match the url")
        prev = self.get(s["id"])
        if prev:
            s["createdAt"] = prev.get("createdAt") or s["createdAt"]
        with self._lock:
            _write_json_atomic(self._dir / f"{s['id']}.json", s)
        return s

    def delete(self, sid: str) -> bool:
        sid2 = sanitize_id(sid)
        if not sid2:
            return False
        p = self._dir / f"{sid2}.json"
        with self._lock:
            if p.exists():
                p.unlink()
                return True
        return False


class SetupNames:
    """Display names for the system setups (the wire codes never change)."""

    def __init__(self, path: Path = _SETUPS_DIR / "names.json") -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, str]:
        d = _read_json(self._path) if self._path.exists() else None
        names = dict(_SYSTEM_DEFAULT_NAMES)
        if isinstance(d, dict):
            for k, v in d.items():
                if k in _SYSTEM_CODES and isinstance(v, str) and v.strip():
                    names[k] = v.strip()[:60]
        return names

    def save(self, patch: dict) -> dict[str, str]:
        cur = self.load()
        for k, v in (patch or {}).items():
            if k in _SYSTEM_CODES:
                v = str(v or "").strip()[:60]
                cur[k] = v or _SYSTEM_DEFAULT_NAMES[k]
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(self._path, {k: cur[k] for k in _SYSTEM_CODES if cur[k] != _SYSTEM_DEFAULT_NAMES[k]})
        return cur

    @staticmethod
    def defaults() -> dict[str, str]:
        return dict(_SYSTEM_DEFAULT_NAMES)


# ── evaluator ────────────────────────────────────────────────────────────────

@dataclass
class _Plan:
    """Immutable compiled view of the enabled setups, swapped atomically on reload."""
    setups: list[dict]
    # (trigger id, option key) -> list of (setup index, trigger cfg)
    keys: dict[tuple[str, str], list[tuple[int, dict]]] = field(default_factory=dict)
    emas: set[tuple[int, int]] = field(default_factory=set)


def _trigger_instance_key(tid: str, opt: str, params: Optional[dict] = None) -> str:
    """Stable identity for one configured trigger, including its parameters."""
    base = trigger_key(tid, opt)
    if not params:
        return base
    encoded = json.dumps(params, sort_keys=True, separators=(",", ":"))
    return f"{base}|{encoded}"


def _compile(setups: list[dict]) -> _Plan:
    plan = _Plan(setups=[s for s in setups if s.get("enabled", True) and s.get("triggers")])
    for i, s in enumerate(plan.setups):
        for t in s["triggers"]:
            tdef = BY_ID.get(t["id"])
            if tdef is None:
                continue
            for o in (t.get("options") or [""]):
                plan.keys.setdefault((t["id"], o), []).append((i, t))
                # pre-register the EMAs a level/option needs so they seed from history
                if t["id"] in ("cross_above", "cross_below", "back_to_ema") and o.startswith("ema") and not o.endswith("_d"):
                    period, tf = o[3:].split("_")
                    plan.emas.add((int(tf), int(period)))
            # ema_cross_ema names its periods in PARAMS, not in the option key,
            # so the option-key scan above cannot see them.
            if t["id"] == "ema_cross_ema":
                q = t.get("params") or {}
                tf = int(q.get("tf", 1))
                plan.emas.add((tf, int(q.get("fast", 3))))
                plan.emas.add((tf, int(q.get("slow", 9))))
        # Parameters read EMAs too. SymbolSeries.ema() seeds from the candle
        # ring when it is created, so a lazily created one already has the right
        # value; registering here just moves that work to warmup instead of
        # paying it on the bar that first asks.
        for c in (s.get("parameters") or []):
            q = c.get("params") or {}
            if c.get("id") == "ema_stack":
                tf = int(q.get("tf", 1))
                plan.emas.add((tf, int(q.get("fast", 8))))
                plan.emas.add((tf, int(q.get("slow", 21))))
            elif c.get("id") == "dist_ema9_pct":
                plan.emas.add((int(c.get("option") or 5), 9))
    return plan


class CustomEvaluator:
    """Evaluates every enabled custom setup on each 1-min bar."""

    def __init__(self, store: Optional[CustomSetupStore] = None, *, sink_cooldown_min: int = 5,
                 series_store: Optional[dict] = None) -> None:
        """
        Args:
            series_store: shared {symbol: SymbolSeries} owned by LiveScanner.
                The multi-timeframe aggregation was always paid for every symbol
                on every bar (series.on_bar runs before the `not plan.keys`
                early out), so sharing costs nothing new, and it lets the
                universe conditions read the series without requiring this
                evaluator to be attached at all. Self-owns one when not given,
                which keeps the standalone check() path and the tests working.
        """
        self.store = store or CustomSetupStore()
        self._series: dict[str, SymbolSeries] = {} if series_store is None else series_store
        self.owns_series: bool = series_store is None
        self._plan: _Plan = _compile(self.store.load_all())
        self._last_fire: dict[tuple[str, str], float] = {}          # (setup, symbol) -> epoch of last alert
        self._last_trig: dict[tuple[str, str, str], float] = {}     # (setup, symbol, key) -> epoch
        self._and_seen: dict[tuple[str, str], dict[str, float]] = {}
        self._stats: dict[str, dict[str, int]] = {}                  # setup -> {trigger key: fires}
        self._last_eval: dict[str, dict[str, dict]] = {}             # symbol -> {key: {fired, value, note, ts}}
        self._since = pd.Timestamp.utcnow().isoformat()
        self._lock = threading.Lock()
        self._sink_cooldown_min = sink_cooldown_min
        # scanner.recent_activity.RecentActivity, set by LiveScanner: records the
        # alerts a don't-repeat timer or an "at least N of" count held back.
        self.activity = None

    # ── config ──
    def reload(self) -> None:
        plan = _compile(self.store.load_all())
        with self._lock:
            self._plan = plan
            for s in self._series.values():
                for tf, period in plan.emas:
                    s.want_ema(tf, period)
        log.info("custom setups reloaded: %d enabled, %d trigger keys", len(plan.setups), len(plan.keys))

    @property
    def plan(self) -> _Plan:
        return self._plan

    # ── warmup ──
    def warmup(self, symbol_daily: dict[str, pd.DataFrame], bars_5m: Optional[dict[str, pd.DataFrame]] = None) -> None:
        """Build and seed the per-symbol series.

        When the series store is shared (LiveScanner owns it), LiveScanner has
        already seeded it and this only registers the EMAs the compiled plan
        needs. Seeding twice would push every historical close through the EMAs
        a second time and silently corrupt them.
        """
        for sym, daily in (symbol_daily or {}).items():
            if sym == "SPY":
                continue
            s = self._series.setdefault(sym, SymbolSeries(sym))
            for tf, period in self._plan.emas:
                s.want_ema(tf, period)
            if self.owns_series:
                s.seed_daily(daily)
                s.seed_intraday((bars_5m or {}).get(sym))
        log.info("custom evaluator warm: %d symbols (series %s)",
                 len(self._series), "owned" if self.owns_series else "shared")

    def series(self, symbol: str) -> SymbolSeries:
        s = self._series.get(symbol)
        if s is None:
            s = self._series[symbol] = SymbolSeries(symbol)
            for tf, period in self._plan.emas:
                s.want_ema(tf, period)
        return s

    def reset(self) -> None:
        self._last_fire.clear()
        self._last_trig.clear()
        self._and_seen.clear()
        self._stats = {}
        self._last_eval = {}
        self._since = pd.Timestamp.utcnow().isoformat()

    def prime_bar(self, state: Any, bar: dict, session: str,
                  external: Optional[set[str]] = None,
                  spy_mom_15m: Optional[float] = None) -> None:
        """Advance trigger memory for a replayed bar without building alerts."""
        plan = self._plan
        if not plan.keys:
            return
        ts = pd.Timestamp(bar["timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        et = ts.tz_convert("America/New_York")
        ctx = EvalCtx(
            state=state,
            series=self.series(state.symbol),
            bar=bar,
            et_min=et.hour * 60 + et.minute,
            session=session,
            external=external or set(),
            spy_mom_15m=spy_mom_15m,
        )
        for (tid, opt), users in plan.keys.items():
            primed: set[str] = set()
            for setup_index, tcfg in users:
                if session not in plan.setups[setup_index].get("sessions", ["rth"]):
                    continue
                pkey = json.dumps(tcfg.get("params") or {}, sort_keys=True)
                if pkey in primed:
                    continue
                primed.add(pkey)
                try:
                    evaluate(tid, ctx, opt, tcfg.get("params") or {})
                except Exception as exc:
                    log.debug("trigger %s:%s prime failed for %s: %s",
                              tid, opt, state.symbol, exc)

    # ── per bar ──
    def on_bar(self, state: Any, bar: dict, external: Optional[set[str]] = None,
               spy_mom_15m: Optional[float] = None, session: Optional[str] = None) -> list[dict]:
        """
        Args:
            session: the session tag from an already-updated shared series.
                When given, the series is NOT advanced here (LiveScanner did
                it). Advancing it twice would double-count every bar's volume.
        """
        plan = self._plan
        sym = state.symbol
        ts = pd.Timestamp(bar["timestamp"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        et = ts.tz_convert("America/New_York")
        em = et.hour * 60 + et.minute
        series = self.series(sym)
        if session is None:
            sess = series.on_bar(bar, em, et.strftime("%Y-%m-%d"), _f(getattr(state, "vwap", None)))
        else:
            sess = session
        if not plan.keys:
            return []
        ctx = EvalCtx(state=state, series=series, bar=bar, et_min=em, session=sess,
                      external=external or set(), spy_mom_15m=spy_mom_15m)
        fires: dict[str, Fire] = {}
        snap: dict[str, dict] = {}
        for (tid, opt), users in plan.keys.items():
            eligible = [
                (setup_index, tcfg)
                for setup_index, tcfg in users
                if sess in plan.setups[setup_index].get("sessions", ["rth"])
            ]
            if not eligible:
                continue
            # every user of this key shares params only when identical; evaluate per distinct params
            done: dict[str, Optional[Fire]] = {}
            for _, tcfg in eligible:
                pkey = json.dumps(tcfg.get("params") or {}, sort_keys=True)
                if pkey in done:
                    continue
                try:
                    f = evaluate(tid, ctx, opt, tcfg.get("params") or {})
                except Exception as exc:
                    log.debug("trigger %s:%s failed for %s: %s", tid, opt, sym, exc)
                    f = None
                done[pkey] = f
                k = _trigger_instance_key(tid, opt, tcfg.get("params"))
                snap[k] = {"fired": bool(f), "value": f.value if f else None, "note": f.note if f else "", "ts": et.isoformat()}
                if f is not None:
                    fires[k] = f
        if snap:
            self._last_eval[sym] = snap
        if not fires:
            return []

        now = ts.timestamp()
        out: list[dict] = []
        for i, s in enumerate(plan.setups):
            if sess not in s.get("sessions", ["rth"]):
                continue
            fired_here: list[tuple[str, str, dict, Fire]] = []
            for t in s["triggers"]:
                for o in (t.get("options") or [""]):
                    instance_key = _trigger_instance_key(t["id"], o, t.get("params"))
                    f = fires.get(instance_key)
                    if f is None:
                        continue
                    if s["direction"] != "all" and f.direction not in (s["direction"], "neutral"):
                        continue
                    display_key = trigger_key(t["id"], o)
                    rep = int(t.get("repeat_sec") or 0)
                    last = self._last_trig.get((s["id"], sym, instance_key))
                    if rep and last is not None and now - last < rep:
                        if self.activity is not None:
                            self.activity.add(sym, ts, "custom", s["id"], f.direction, "repeat", display_key,
                                              [f"this alert fired {int(now - last)}s ago "
                                               f"(don't repeat for {rep}s)"])
                        continue
                    fired_here.append((instance_key, display_key, t, f))
            if not fired_here:
                continue
            if s["mode"] in ("and", "atleast"):
                seen = self._and_seen.setdefault((s["id"], sym), {})
                for instance_key, _, _, _ in fired_here:
                    seen[instance_key] = now
                window = s.get("and_window_min", 5) * 60
                need = {
                    _trigger_instance_key(t["id"], o, t.get("params"))
                    for t in s["triggers"] for o in (t.get("options") or [""])
                }
                have = sum(1 for k in need if seen.get(k, -1e18) >= now - window)
                # AND is "atleast" with the count pinned to every alert, so one
                # branch serves both and they cannot drift apart.
                want = len(need) if s["mode"] == "and" else min(
                    max(1, int(s.get("min_triggers", 2))), len(need))
                if have < want:
                    if self.activity is not None:
                        self.activity.add(sym, ts, "custom", s["id"], fired_here[0][3].direction,
                                          "waiting", ", ".join(k for _, k, _, _ in fired_here),
                                          [f"{have} of {want} alerts within "
                                           f"{s.get('and_window_min', 5):g} min"])
                    continue
                self._and_seen[(s["id"], sym)] = {}
            rep_s = int(s.get("repeat_sec") or 0)
            last_s = self._last_fire.get((s["id"], sym))
            if rep_s and last_s is not None and now - last_s < rep_s:
                if self.activity is not None:
                    self.activity.add(sym, ts, "custom", s["id"], fired_here[0][3].direction, "repeat",
                                      ", ".join(k for _, k, _, _ in fired_here),
                                      [f"setup fired {int(now - last_s)}s ago (don't repeat for {rep_s}s)"])
                continue
            self._last_fire[(s["id"], sym)] = now
            for instance_key, _, _, _ in fired_here:
                self._last_trig[(s["id"], sym, instance_key)] = now
                counts = self._stats.setdefault(s["id"], {})
                counts[instance_key] = counts.get(instance_key, 0) + 1
            # one alert per setup per bar; the strongest note goes first
            _, k0, _, f0 = fired_here[0]
            out.append(self._build_alert(
                state, bar, et, s, k0, f0,
                [display_key for _, display_key, _, _ in fired_here], sess))
        return out

    # ── alert ──
    def _build_alert(self, state: Any, bar: dict, et: pd.Timestamp, s: dict, key: str, f: Fire,
                     all_keys: list[str], sess: str) -> dict:
        from scanner.stops import compute_stop   # local import: keeps module import order simple
        series = self.series(state.symbol)
        price = float(bar["close"])
        direction = s.get("alert_direction") or (
            f.direction if f.direction in ("long", "short") else (
                "long" if s["direction"] == "long" else "short" if s["direction"] == "short" else "neutral"))
        stop = stop_pct = None
        if sess == "rth" and direction in ("long", "short"):
            try:
                stop, stop_pct = compute_stop(state, bar, direction)
            except Exception:
                stop = stop_pct = None
        rvol = _f(getattr(state, "rvol", None))
        score = 55
        if rvol:
            score += int(min(25, max(0, (rvol - 1.0) * 10)))
        if len(all_keys) > 1:
            score += min(15, 5 * (len(all_keys) - 1))
        score = max(0, min(100, score))
        tdef = BY_ID.get(key.split(":")[0]) if ":" in key else BY_ID.get(key)
        tid, _, opt = key.partition(":") if not key.startswith("setup:") else (key, "", "")
        if key.startswith("setup:"):
            parts = key.split(":")
            tid = ":".join(parts[:2]); opt = parts[2] if len(parts) > 2 else ""
        label = describe(tid, opt, {}) if BY_ID.get(tid) else key
        return {
            "symbol": state.symbol,
            "direction": direction,
            "timestamp": et.isoformat(),
            "price": price,
            "trigger": f"CS_{s['id']}:{key}",
            "triggers_fired": all_keys,
            "score": score,
            "market_regime": "neutral",
            "conditions": {},
            "vwap": _f(getattr(state, "vwap", None)),
            "ema3": _f(series.ema(5, 3).value),
            "ema9": _f(series.ema(5, 9).value),
            "ema21": _f(series.ema(5, 21).value),
            "pct_change": (price / state.prior_close - 1.0) if getattr(state, "prior_close", None) else None,
            "rvol": rvol,
            # system-feed payload fields
            "setup": s["id"],
            "setup_label": s["name"],
            "setup_color": s.get("color"),
            "custom": True,
            "tier": None,
            "entry_trigger": key,
            "trigger_label": label,
            "trigger_note": f.note,
            "trigger_value": f.value,
            "size_hint": s.get("size_hint", "half"),
            "suggested_stop": stop,
            "stop_pct": stop_pct,
            "stop_ok": (stop_pct is not None and stop_pct <= 1.0) if stop_pct is not None else None,
            "management": [],
            "warnings": [],
            "gates": [],
            "session": sess,
            "context": {
                "gap_pct": _f(getattr(state, "gap_pct", None)),
                "rvol": rvol,
                "dist_vwap_pct": _f(getattr(state, "dist_vwap_pct", None)),
                "mom_15m_pct": _f(getattr(state, "mom_15m_pct", None)),
                "adv20": _f(getattr(state, "adv20", None)),
                "day_range_pos": _f(getattr(state, "day_range_pos", None)),
                "prior_close": _f(getattr(state, "prior_close", None)),
                "session_open": _f(getattr(state, "session_open", None)),
                "hod": _f(getattr(state, "high_of_day", None)),
                "lod": _f(getattr(state, "low_of_day", None)),
            },
        }

    # ── introspection (API) ──
    def stats(self) -> dict:
        return {"since": self._since, "setups": {k: dict(v) for k, v in self._stats.items()}}

    def check(self, setup: dict, state: Any) -> dict:
        """Stock check: per configured trigger, the last evaluation for this symbol
        plus the live level values it compares against."""
        sym = state.symbol
        series = self.series(sym)
        last = self._last_eval.get(sym, {})
        rows: list[dict] = []
        display_counts: dict[str, int] = {}
        for configured in setup.get("triggers", []):
            for configured_option in (configured.get("options") or [""]):
                display = trigger_key(configured["id"], configured_option)
                display_counts[display] = display_counts.get(display, 0) + 1
        ctx = EvalCtx(state=state, series=series, bar={"open": 0, "high": 0, "low": 0, "close": _f(getattr(state, "_last_close", None)) or 0},
                      et_min=0, session="rth", external=set())
        for t in setup.get("triggers", []):
            tdef = BY_ID.get(t["id"])
            for o in (t.get("options") or [""]):
                display_key = trigger_key(t["id"], o)
                instance_key = _trigger_instance_key(t["id"], o, t.get("params"))
                ev = last.get(instance_key) or {}
                level = None
                state_note = None
                try:
                    if t["id"] in ("cross_above", "cross_below") or t["id"] == "back_to_ema":
                        level = ctx.level(o if not o.startswith("ema") or "_" in o else o)
                    elif t["id"] in ("prior_day_break",):
                        level = ctx.level("prior_high" if o == "high" else "prior_low")
                    elif t["id"] in ("pm_break",):
                        level = ctx.level("pm_high" if o == "high" else "pm_low")
                    elif t["id"] == "hod":
                        level = series.day_high if o == "high" else series.day_low
                    elif t["id"] in ("break_recent_high", "near_last_high", "reject_last_high"):
                        level = series.swing(int(o), int((t.get("params") or {}).get("lookback", 5)), "high")
                    elif t["id"] in ("break_recent_low", "near_last_low", "reject_last_low"):
                        level = series.swing(int(o), int((t.get("params") or {}).get("lookback", 5)), "low")
                    elif t["id"] == "hi_lo_60d":
                        days = int((t.get("params") or {}).get("days", 60))
                        level = n_day_level(series, state, days, o)
                        if series.mem.get(n_day_latch_key(days, o)):
                            state_note = f"already alerted for the {o} side this trading day"
                    elif t["id"] == "hi_lo_52w":
                        level = series.daily.get("hi_52w" if o == "high" else "lo_52w")
                except Exception:
                    level = None
                rows.append({
                    "key": (instance_key if display_counts[display_key] > 1 else display_key),
                    "label": describe(t["id"], o, t.get("params") or {}),
                    "source": tdef.source if tdef else "native",
                    "fired_last_bar": bool(ev.get("fired")), "value": ev.get("value"),
                    "note": ev.get("note") or state_note,
                    "level": level, "last_eval": ev.get("ts"),
                    "fires_today": self._stats.get(setup["id"], {}).get(instance_key, 0),
                })
        return {
            "symbol": sym,
            "in_universe": True,
            "price": _f(getattr(state, "_last_close", None)),
            "session_open": _f(getattr(state, "session_open", None)),
            "vwap": _f(getattr(state, "vwap", None)),
            "rvol": _f(getattr(state, "rvol", None)),
            "bars_1m": len(series.m1),
            "candles": {str(tf): len(series.candles[tf]) for tf in series.candles},
            "triggers": rows,
            "enabled": bool(setup.get("enabled", True)),
            "session_ok": True,
        }


def _f(v) -> Optional[float]:
    try:
        if v is None:
            return None
        x = float(v)
        return x if x == x and abs(x) != float("inf") else None
    except Exception:
        return None


__all__ = ["CustomSetupStore", "SetupNames", "CustomEvaluator", "SetupError", "normalize_setup", "summary_lines"]
_ = time  # keep import for callers that monkeypatch time in tests
