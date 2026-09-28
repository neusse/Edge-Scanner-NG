"""Universe profiles: named, reusable condition sets that setups point at.

The model, in three pieces:

  base universe   the symbols that get warmed up and streamed. A floor, not a
                  selector. `data/universe.csv`, unchanged.
  profile         a named AND-list of conditions (scanner/conditions.py), owned
                  here, edited in the dashboard's Universe panel.
  assignment      each setup names one profile. Custom setups carry the id in
                  their own JSON; system setups and toplists use
                  SetupProfiles below.

When a setup would fire, its profile is checked before the alert is emitted.
That placement is what makes this cheap: a profile is consulted only on the
handful of would-be alerts per bar, never on every symbol.

## The static / dynamic split

Static conditions (price, adv20, dollar volume, ATR%, float) cannot change
during the session, so each profile resolves them once into a member set and
checking them afterwards is a hash lookup. Dynamic conditions (RVOL, relative
N-min volume, streaks, distance from VWAP/EMA9) are resolved at fire time. A
profile mixing both passes iff the symbol is a member AND every dynamic
condition holds.

Because a static condition's truth value cannot change during the session, the
member set is a pure optimisation: it produces the same answer as evaluating
the condition at fire time would.

## Hot reload

Saving a profile recompiles and swaps atomically under a lock, exactly like
`CustomEvaluator.reload()`. A save applies on the next bar with no restart.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from scanner.conditions import (
    CATALOG,
    ConditionCtx,
    ConditionError,
    check as check_condition,
    describe as describe_condition,
    normalize_condition,
)
from scanner.json_store import _read_json, _write_json_atomic, sanitize_id
from scanner import plugins
from scanner.gates import GateCheck

log = logging.getLogger(__name__)

_PROFILES_DIR = Path("data/universe/profiles")
_DEFAULTS_FILE = Path(__file__).with_name("universe_profiles_defaults.json")
_ASSIGN_FILE = Path("data/setups/profiles.json")

# Assignment keys mirror the scoping vocabulary already used by Param.setups:
# the system setup codes.
SYSTEM_KEYS = plugins.SYSTEM_CODES

# Keys beyond the always-present defaults above. Rankings (toplists) are
# assignable on the same mechanism so the "used by" count in the Universe list
# covers them too. Custom setups carry their filter on the setup document.
# Keys that no longer match (from retired feeds) are dropped on load.
_EXTRA_KEY_RE = re.compile(r"^toplist:[A-Za-z0-9_.-]{1,64}$")


def valid_assignment_key(key: str) -> bool:
    """True for a key `SetupProfiles` will store."""
    return key in SYSTEM_KEYS or bool(_EXTRA_KEY_RE.match(key or ""))

# The profile every assignment defaults to: no conditions, passes everything.
# Day-one behaviour after this ships is therefore "no enforcement anywhere".
ALL_ID = "up_all"


class ProfileError(ValueError):
    """Raised when a profile cannot be normalized."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── normalize ────────────────────────────────────────────────────────────────

# A stored condition list is one of two things, and the only difference is which
# half of the catalog it may hold. Keeping them in one shape means one editor,
# one validator and one evaluator rather than two of each.
KIND_UNIVERSE = "universe"      # static only:  what KIND of stock is this
KIND_PARAMS = "parameters"      # dynamic only: what is it doing RIGHT NOW
_OTHER_HALF = {KIND_UNIVERSE: "setup parameter, not a universe filter. Add it on "
                              "the setup's Parameters tab",
               KIND_PARAMS: "universe condition, not a parameter: it cannot change "
                            "during the session. Put it in a universe filter instead"}


def normalize_profile(raw: dict, *, existing_id: Optional[str] = None,
                      kind: str = KIND_UNIVERSE) -> dict:
    """Validate one profile document. Raises ProfileError."""
    if not isinstance(raw, dict):
        raise ProfileError("profile must be an object")

    pid = sanitize_id(raw.get("id") or existing_id or "")
    if not pid:
        raise ProfileError("profile id must match [A-Za-z0-9_-]{1,64}")

    name = str(raw.get("name") or "").strip() or pid
    if len(name) > 80:
        name = name[:80]

    conds: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for c in (raw.get("conditions") or []):
        nc = normalize_condition(c)             # raises ConditionError
        d = CATALOG.get(nc["id"])
        want = "static" if kind == KIND_UNIVERSE else "dynamic"
        if d is not None and d.kind != want:
            # Letting both kinds into one list produced nine "universe filters"
            # holding no universe at all, and cost the member-set optimisation
            # that only static conditions can use.
            raise ProfileError(f"{d.name} is a {_OTHER_HALF[kind]}.")
        key = (nc["id"], nc.get("option", ""))
        if key in seen:
            raise ProfileError(
                f"{nc['id']} appears twice with the same option; conditions are ANDed, "
                "so use one row with the tighter value")
        seen.add(key)
        conds.append(nc)
    if len(conds) > 20:
        raise ProfileError("a profile is limited to 20 conditions")

    return {
        "id": pid,
        "name": name,
        "kind": kind,
        "desc": str(raw.get("desc") or "")[:400],
        "color": str(raw.get("color") or "#3b82f6")[:16],
        "conditions": conds,
        "source": str(raw.get("source") or "user")[:32],
        "createdAt": str(raw.get("createdAt") or _now()),
        "updatedAt": _now(),
    }


def profile_hash(profile: dict) -> str:
    """8 hex over the id and conditions, mirroring Settings._recompute_hash.

    Rides on alerts as `profile_hash` so a signal produced under an edited
    profile is distinguishable in any downstream record. It is
    deliberately NOT folded into `config_hash`, which already has settings-only
    semantics that consumers have stored against.
    """
    payload = json.dumps({"id": profile.get("id"), "conditions": profile.get("conditions", [])},
                         sort_keys=True)
    return hashlib.sha1(payload.encode()).hexdigest()[:8]


def summary_lines(profile: dict) -> list[str]:
    return [describe_condition(c) for c in profile.get("conditions", [])]


# ── store ────────────────────────────────────────────────────────────────────

class ProfileStore:
    """One JSON file per profile; seeded from the defaults file when empty."""

    def __init__(self, dir: Path = _PROFILES_DIR, defaults: Path = _DEFAULTS_FILE,
                 kind: str = KIND_UNIVERSE) -> None:
        self._dir = Path(dir)
        self._defaults = Path(defaults)
        self.kind = kind
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
            for raw in (data or {}).get("profiles", []):
                try:
                    p = normalize_profile(raw, kind=self.kind)
                    _write_json_atomic(self._dir / f"{p['id']}.json", p)
                except Exception as exc:
                    log.warning("seed profile skipped: %s", exc)
        except Exception as exc:
            log.warning("profile seeding failed: %s", exc)

    def load_all(self) -> list[dict]:
        out: list[dict] = []
        with self._lock:
            for p in sorted(self._dir.glob("*.json")):
                d = _read_json(p)
                if isinstance(d, dict) and d.get("id"):
                    out.append(d)
        out.sort(key=lambda s: (s.get("id") != ALL_ID, s.get("name") or ""))
        return out

    def get(self, pid: str) -> Optional[dict]:
        sid = sanitize_id(pid)
        if not sid:
            return None
        d = _read_json(self._dir / f"{sid}.json")
        return d if isinstance(d, dict) else None

    def save(self, raw: dict, *, pid: Optional[str] = None) -> dict:
        p = normalize_profile(raw, existing_id=pid, kind=self.kind)
        if pid and p["id"] != pid:
            raise ProfileError("id in body does not match the url")
        prev = self.get(p["id"])
        if prev:
            p["createdAt"] = prev.get("createdAt") or p["createdAt"]
        with self._lock:
            _write_json_atomic(self._dir / f"{p['id']}.json", p)
        return p

    def delete(self, pid: str) -> bool:
        sid = sanitize_id(pid)
        if not sid or (sid == ALL_ID and self.kind == KIND_UNIVERSE):
            return False
        path = self._dir / f"{sid}.json"
        with self._lock:
            if path.exists():
                path.unlink()
                return True
        return False


_PARAMSETS_DIR = Path("data/setups/paramsets")
_PARAMSET_DEFAULTS = Path(__file__).with_name("parameter_sets_defaults.json")


class ParamSetStore(ProfileStore):
    """Named, reusable AND-lists of DYNAMIC conditions.

    The mirror of a universe filter on the other half of the catalog. A setup
    points at one and keeps its own inline parameters as extras; the two are
    ANDed, so a setup can TIGHTEN a shared value but never loosen it. That is
    the same one-way property a universe filter has, and it is what makes a
    shared set safe to edit: it can only ever remove alerts from the setups
    using it.

    Kept in its own directory rather than mixed in with the universe filters so
    an id cannot mean two things and `up_all` stays universe-only.
    """

    def __init__(self, dir: Path = _PARAMSETS_DIR,
                 defaults: Path = _PARAMSET_DEFAULTS) -> None:
        super().__init__(dir, defaults, kind=KIND_PARAMS)


class SetupProfiles:
    """Which profile each system setup / toplist uses.

    Kept out of settings.SCHEMA deliberately: those keys are global and
    name-encoded, and `Settings.coerce` rejects anything non-numeric, so a
    string profile id cannot live there at all.
    """

    def __init__(self, path: Path = _ASSIGN_FILE) -> None:
        self._path = Path(path)
        self._lock = threading.Lock()

    def load(self) -> dict[str, str]:
        """Every default key, plus whatever individual scopes have been stored."""
        d = _read_json(self._path) if self._path.exists() else None
        out = {k: ALL_ID for k in SYSTEM_KEYS}
        if isinstance(d, dict):
            for k, v in d.items():
                if valid_assignment_key(k) and isinstance(v, str) and v.strip():
                    out[k] = v.strip()
        return out

    def save(self, mapping: dict) -> dict[str, str]:
        cur = self.load()
        for k, v in (mapping or {}).items():
            if not valid_assignment_key(k):
                raise ProfileError(
                    f"unknown setup key: {k!r} (expected one of {list(SYSTEM_KEYS)}, "
                    "or toplist: followed by a name)")
            if v is None or (isinstance(v, str) and not v.strip()):
                cur[k] = ALL_ID
            elif isinstance(v, str):
                cur[k] = v.strip()
            else:
                raise ProfileError(f"{k}: profile id must be a string or null")
        # Keys left at the default carry no information: dropping them keeps the
        # file readable and lets a later default change take effect.
        cur = {k: v for k, v in cur.items() if v != ALL_ID}
        with self._lock:
            _write_json_atomic(self._path, cur)
        return self.load()

    def for_setup(self, code: str) -> str:
        """Resolve a setup code to a profile id, honouring `<prefix>:*` wildcards."""
        m = self.load()
        if code in m:
            return m[code]
        if ":" in code:
            return m.get(f"{code.split(':')[0]}:*", ALL_ID)
        return ALL_ID


# ── compiled form + engine ───────────────────────────────────────────────────

@dataclass
class CompiledProfile:
    id: str
    name: str
    hash: str
    static: list[dict] = field(default_factory=list)
    dynamic: list[dict] = field(default_factory=list)
    members: Optional[frozenset[str]] = None      # None until resolved this session

    @property
    def is_empty(self) -> bool:
        return not self.static and not self.dynamic


def compile_profile(profile: dict) -> CompiledProfile:
    static, dynamic = [], []
    for c in profile.get("conditions", []):
        d = CATALOG.get(c["id"])
        if d is None:
            continue
        (static if d.kind == "static" else dynamic).append(c)
    return CompiledProfile(id=profile["id"], name=profile.get("name") or profile["id"],
                           hash=profile_hash(profile), static=static, dynamic=dynamic)


@dataclass
class ProfileResult:
    profile_id: str
    name: str
    hash: str
    passed: bool
    checks: list[GateCheck] = field(default_factory=list)

    def to_json(self) -> dict:
        return {
            "id": self.profile_id, "name": self.name, "hash": self.hash,
            "passed": self.passed,
            "checks": [{"name": c.name, "passed": c.passed, "value": c.value,
                        "reason": c.reason} for c in self.checks],
        }


class BarProfileCache:
    """Per (symbol, bar) memo so shared conditions evaluate once.

    Six setups pointing at the same profile and firing on the same bar for the
    same symbol should cost one evaluation, not six. Two different profiles
    sharing `rvol >= 1.0` should also cost one. Keyed on the condition itself,
    mirroring the params-hash de-dup CustomEvaluator already does for triggers.
    Discarded when the bar is done, so there is nothing to invalidate.
    """

    __slots__ = ("_v",)

    def __init__(self) -> None:
        self._v: dict[str, GateCheck] = {}

    def check(self, cond: dict, ctx: ConditionCtx) -> GateCheck:
        key = f"{cond['id']}|{cond.get('option','')}|{cond['op']}|{cond['value']}|" \
              f"{json.dumps(cond.get('params') or {}, sort_keys=True)}"
        got = self._v.get(key)
        if got is None:
            got = self._v[key] = check_condition(cond, ctx)
        return got


class ProfileEngine:
    """Holds the compiled profiles and the session member sets."""

    def __init__(self, store: Optional[ProfileStore] = None,
                 assignments: Optional[SetupProfiles] = None,
                 param_sets: Optional[ParamSetStore] = None) -> None:
        self.store = store or ProfileStore()
        self.assignments = assignments or SetupProfiles()
        self.param_sets = param_sets or ParamSetStore()
        self._sets: dict[str, list[dict]] = {}
        self._lock = threading.Lock()
        self._compiled: dict[str, CompiledProfile] = {}
        self._assign: dict[str, str] = {}
        self.reload()

    # -- config --
    def reload(self) -> None:
        compiled = {p["id"]: compile_profile(p) for p in self.store.load_all()}
        compiled.setdefault(ALL_ID, CompiledProfile(ALL_ID, "All symbols", "default"))
        assign = self.assignments.load()
        sets = {p["id"]: list(p.get("conditions") or []) for p in self.param_sets.load_all()}
        with self._lock:
            self._compiled = compiled
            self._assign = assign
            self._sets = sets
        log.info("universe profiles reloaded: %d (%s)", len(compiled), ", ".join(sorted(compiled)))

    @property
    def compiled(self) -> dict[str, CompiledProfile]:
        return self._compiled

    def get(self, pid: Optional[str]) -> Optional[CompiledProfile]:
        if not pid:
            return None
        return self._compiled.get(pid)

    def for_setup(self, code: str, custom_profile: Optional[str] = None) -> Optional[CompiledProfile]:
        """The profile a setup should be checked against.

        `custom_profile` is the id carried on a custom setup's own document; it
        wins when present. Everything else resolves through the assignment map.
        """
        if custom_profile:
            return self.get(custom_profile)
        m = self._assign
        pid = m.get(code)
        if pid is None and ":" in code:
            pid = m.get(f"{code.split(':')[0]}:*")
        return self.get(pid or ALL_ID)

    # -- static membership --
    def resolve_members(self, states: dict[str, Any],
                        fundamentals: Optional[dict[str, dict]] = None) -> None:
        """Resolve every profile's static half into a member set, once per session.

        Called at the end of warmup and again whenever a profile is saved. A
        symbol with no SymbolState is absent from every set and can therefore
        never fire, which is the correct outcome.
        """
        funds = fundamentals or {}
        with self._lock:
            profiles = list(self._compiled.values())
        for cp in profiles:
            if not cp.static:
                cp.members = None                 # nothing static: everyone passes
                continue
            members = set()
            for sym, st in states.items():
                ctx = ConditionCtx(state=st, series=None, bar=None,
                                   fundamentals=funds.get(sym))
                if all(check_condition(c, ctx).passed for c in cp.static):
                    members.add(sym)
            cp.members = frozenset(members)
            log.info("profile %s: %d / %d symbols pass the static conditions",
                     cp.id, len(members), len(states))

    def resolve_symbol(self, symbol: str, state: Any,
                       fundamentals: Optional[dict] = None) -> None:
        """Add or remove one mid-session symbol from every static member set.

        Dynamic admission must not rebuild the full universe or invalidate the
        already-resolved membership of existing symbols.  Copy-on-write keeps
        readers on a coherent frozen set while this one symbol is evaluated.
        Profiles without static conditions continue to use ``members=None``.
        """
        symbol = str(symbol).upper()
        with self._lock:
            profiles = list(self._compiled.values())
        for cp in profiles:
            if not cp.static:
                continue
            if cp.members is None:
                # The initial cohort has not been resolved yet. Leave the
                # profile on its existing inline-evaluation path instead of
                # manufacturing a one-symbol member set.
                continue
            ctx = ConditionCtx(state=state, series=None, bar=None,
                               fundamentals=fundamentals)
            passed = all(check_condition(condition, ctx).passed for condition in cp.static)
            members = set(cp.members)
            if passed:
                members.add(symbol)
            else:
                members.discard(symbol)
            cp.members = frozenset(members)

    def invalidate_members(self) -> None:
        for cp in self._compiled.values():
            cp.members = None

    def members(self, pid: str) -> Optional[frozenset[str]]:
        cp = self.get(pid)
        return cp.members if cp else None

    def params_for(self, set_id: Optional[str]) -> list[dict]:
        """The conditions in a named parameter set, empty when it has none or
        the id is unknown. An unknown id passes rather than blocking, matching
        how an unknown universe profile behaves, so deleting a set cannot
        silence every setup that referenced it."""
        if not set_id:
            return []
        return list(self._sets.get(set_id, ()))

    def check_conditions(self, conds: Iterable[dict], ctx: ConditionCtx,
                         cache: Optional[BarProfileCache] = None) -> ProfileResult:
        """An ad-hoc AND-list, for a setup's own parameters.

        Same evaluation as a profile's dynamic half, with no member set: these
        belong to one setup and are never shared, so there is nothing to cache
        across the session. The per-bar cache still de-dups a condition two
        setups happen to share on the same bar.
        """
        conds = list(conds)
        if not conds:
            return ProfileResult("params", "Parameters", "-", True, [])
        cp = CompiledProfile("params", "Parameters", "-", static=[], dynamic=conds)
        return self.check(cp, ctx, cache)

    # -- fire-time check --
    def check(self, cp: Optional[CompiledProfile], ctx: ConditionCtx,
              cache: Optional[BarProfileCache] = None) -> ProfileResult:
        """Evaluate a profile for one symbol on one bar."""
        if cp is None or cp.is_empty:
            pid = cp.id if cp else ALL_ID
            name = cp.name if cp else "All symbols"
            return ProfileResult(pid, name, cp.hash if cp else "default", True, [])

        checks: list[GateCheck] = []
        passed = True
        sym = getattr(ctx.state, "symbol", "")

        if cp.static:
            if cp.members is None:
                # Not resolved yet (no warmup, or invalidated). Evaluate inline
                # rather than assuming a pass: the answer is identical, just
                # slower, and assuming would let an unvetted symbol fire.
                for c in cp.static:
                    got = cache.check(c, ctx) if cache else check_condition(c, ctx)
                    checks.append(got)
                    passed = passed and bool(got.passed)
            elif sym in cp.members:
                # Fast path: one hash lookup stands in for every static
                # condition. Same answer, since none of them can change during
                # the session.
                checks.append(GateCheck("universe_static", True, None, "in profile"))
            else:
                # Blocked. Spend the evaluation to say WHICH condition blocked,
                # rather than reporting an opaque "not in profile". Blocks are
                # rare next to passes, and an unattributed block is exactly the
                # thing that makes a screen impossible to tune.
                passed = False
                for c in cp.static:
                    got = cache.check(c, ctx) if cache else check_condition(c, ctx)
                    checks.append(got)

        for c in cp.dynamic:
            got = cache.check(c, ctx) if cache else check_condition(c, ctx)
            checks.append(got)
            passed = passed and bool(got.passed)

        return ProfileResult(cp.id, cp.name, cp.hash, passed, checks)

    # -- payload --
    def payload(self) -> dict:
        """Everything the dashboard's Universe panel needs in one fetch."""
        profiles = self.store.load_all()
        return {
            "profiles": [dict(p, hash=profile_hash(p), summary=summary_lines(p)) for p in profiles],
            "parameter_sets": [dict(p, hash=profile_hash(p), summary=summary_lines(p))
                               for p in self.param_sets.load_all()],
            "assignments": self.assignments.load(),
            "system_keys": list(SYSTEM_KEYS),
            "members": {pid: (len(cp.members) if cp.members is not None else None)
                        for pid, cp in self._compiled.items()},
        }


class ProfileStats:
    """Per-setup counters for profile checks.

    Deliberately NOT folded into `settings.gate_stats`. Gates are evaluated on
    every symbol on every bar, so their pass rate is a percentage over
    thousands. A profile is only consulted once a setup has already decided to
    fire, so its counts are over would-be alerts, a handful per day. Showing
    both as "% pass" in the same UI would make two different things look
    comparable. "Checked N times when a setup was about to fire, blocked M" is
    the honest phrasing, and the more actionable one.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._since = _now()
        self._checked: dict[str, int] = {}
        self._blocked: dict[str, int] = {}
        self._by_cond: dict[str, dict[str, int]] = {}

    def record(self, setup: str, result: "ProfileResult") -> None:
        with self._lock:
            self._checked[setup] = self._checked.get(setup, 0) + 1
            if result.passed:
                return
            self._blocked[setup] = self._blocked.get(setup, 0) + 1
            d = self._by_cond.setdefault(setup, {})
            for c in result.checks:
                if not c.passed:
                    d[c.name] = d.get(c.name, 0) + 1

    def reset(self) -> None:
        with self._lock:
            self._since = _now()
            self._checked.clear()
            self._blocked.clear()
            self._by_cond.clear()

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "since": self._since,
                "setups": {
                    s: {"checked": n, "blocked": self._blocked.get(s, 0),
                        "by_condition": dict(self._by_cond.get(s, {}))}
                    for s, n in sorted(self._checked.items())
                },
            }


# Process-wide singleton, mirroring settings.gate_stats.
profile_stats = ProfileStats()


__all__ = ["ALL_ID", "KIND_PARAMS", "KIND_UNIVERSE", "ParamSetStore", "SYSTEM_KEYS", "valid_assignment_key", "BarProfileCache", "CompiledProfile", "ProfileEngine",
           "ProfileError", "ProfileResult", "ProfileStats", "ProfileStore", "SetupProfiles",
           "compile_profile", "normalize_profile", "profile_hash", "profile_stats",
           "summary_lines"]
