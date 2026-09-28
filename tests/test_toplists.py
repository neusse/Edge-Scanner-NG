"""Toplists: NaN hygiene, the universe filter, and the Config routes.

`build_rows` used to let NaN through: `st.rvol` is NaN for a symbol with no
volume profile, NaN is not None, and it then ranked against real values and
serialised as null. Harmless on a 243-symbol universe, dominant on a 6,455-one.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from scanner import plugins
from scanner.profiles import (
    ALL_ID, ProfileEngine, ProfileError, ProfileStore, SetupProfiles,
    valid_assignment_key,
)
from scanner.toplists import (
    FILTER_SCOPE, FILTERABLE_LISTS, PREMARKET_LISTS, STREAM_LISTS, TOPLIST_LABEL,
    TOPLISTS, ToplistEngine, ToplistSettings, assignment_key, build_rows,
    member_predicate,
)


class FakeState:
    # `symbol` matters: ProfileEngine.check resolves static membership by it.
    def __init__(self, rvol=1.0, prior_close=100.0, rth_chg_pct=1.0, adv20=20_000_000.0,
                 symbol="X"):
        self.symbol = symbol
        self.rvol = rvol
        self.prior_close = prior_close
        self.rth_chg_pct = rth_chg_pct
        self.adv20 = adv20
        self.atr_d1 = 2.0
        self._cum_vol = 500_000.0
        self._last_close = 105.0
        self.last_1m = []


def _rows(states, name="rvol", **kw):
    return build_rows(states, name, 25, price_fn=lambda st: st._last_close, **kw)


# ── NaN hygiene ──────────────────────────────────────────────────────────────

def test_nan_metric_row_is_dropped():
    states = [("GOOD", FakeState(rvol=3.0, symbol="GOOD")),
              ("WARRANT", FakeState(rvol=float("nan"), symbol="WARRANT"))]
    rows = _rows(states)
    assert [r["symbol"] for r in rows] == ["GOOD"]


def test_nan_never_reaches_the_payload():
    states = [("A", FakeState(rvol=2.0, prior_close=float("nan")))]
    rows = _rows(states)
    assert rows and rows[0]["chg_pct"] is None       # not NaN


def test_non_finite_price_row_is_dropped():
    st = FakeState()
    st._last_close = float("inf")
    assert _rows([("A", st)]) == []


# ── the universe filter ──────────────────────────────────────────────────────

def test_keep_predicate_filters_before_ranking():
    seen: list[str] = []

    def keep(sym, st):
        seen.append(sym)
        return sym != "NO"

    rows = _rows([("YES", FakeState(rvol=1.0, symbol="YES")),
                  ("NO", FakeState(rvol=9.0, symbol="NO"))], keep=keep)
    assert [r["symbol"] for r in rows] == ["YES"]     # the higher value was filtered out
    assert seen == ["YES", "NO"]


def test_engine_applies_the_assigned_profile(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles")
    store.save({"id": "up_liquid", "name": "Liquid",
                "conditions": [{"id": "avg_vol_20d", "op": "gte", "value": 10_000_000}]})
    assign = SetupProfiles(tmp_path / "profiles.json")
    assign.save({assignment_key("rvol"): "up_liquid"})
    eng = ProfileEngine(store, assign)

    states = {"THICK": FakeState(rvol=1.0, adv20=50_000_000.0, symbol="THICK"),
              "THIN": FakeState(rvol=9.0, adv20=100_000.0, symbol="THIN")}
    eng.resolve_members(states)

    class Scanner:
        _states = states
        _series: dict = {}
        _profiles = eng

    top = ToplistEngine(Scanner(), ttl=0.0, settings=ToplistSettings(tmp_path / "tl.json"))
    out = top.compute("rvol", 25)
    assert [r["symbol"] for r in out["rows"]] == ["THICK"]
    assert out["universe"]["id"] == "up_liquid"
    assert out["scanned"] == 2


def test_engine_without_a_profile_engine_is_unfiltered(tmp_path: Path):
    class Scanner:
        _states = {"A": FakeState(rvol=2.0, symbol="A")}
        _series: dict = {}

    top = ToplistEngine(Scanner(), ttl=0.0, settings=ToplistSettings(tmp_path / "tl.json"))
    out = top.compute("rvol", 25)
    assert [r["symbol"] for r in out["rows"]] == ["A"]
    assert out["universe"]["id"] is None


def test_dynamic_symbol_enters_rankings_only_after_readiness(tmp_path: Path):
    class Scanner:
        _states = {"BASE": FakeState(rvol=1.0, symbol="BASE"),
                   "NEW": FakeState(rvol=9.0, symbol="NEW")}
        _series: dict = {}
        ready = False

        def dynamic_ranking_enabled(self, symbol):
            return symbol != "NEW" or self.ready

    scanner = Scanner()
    top = ToplistEngine(scanner, ttl=0.0, settings=ToplistSettings(tmp_path / "tl.json"))
    assert [row["symbol"] for row in top.compute("rvol")["rows"]] == ["BASE"]
    scanner.ready = True
    assert [row["symbol"] for row in top.compute("rvol")["rows"]] == ["NEW", "BASE"]


# ── assignment keys ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["toplist:rvol"])
def test_valid_assignment_keys(key):
    assert valid_assignment_key(key)


@pytest.mark.parametrize("key", list(plugins.SYSTEM_CODES) or [
    pytest.param(None, marks=pytest.mark.skip(reason="no engine plugin installed"))])
def test_installed_system_codes_are_valid_assignment_keys(key):
    assert valid_assignment_key(key)


def test_system_codes_are_valid_assignment_keys(fake_plugin):
    assert all(valid_assignment_key(k) for k in fake_plugin)
    assert not valid_assignment_key("X3")


@pytest.mark.parametrize("key", ["", "nope", "cs_thing", "other:x", "toplist:"])
def test_invalid_assignment_keys(key):
    assert not valid_assignment_key(key)


def test_unknown_assignment_key_is_rejected(tmp_path: Path):
    a = SetupProfiles(tmp_path / "profiles.json")
    with pytest.raises(ProfileError):
        a.save({"bogus": "up_x"})


def test_defaults_are_not_persisted(tmp_path: Path, fake_plugin):
    import json
    path = tmp_path / "profiles.json"
    a = SetupProfiles(path)
    a.save({"X1": "up_x", "X2": ALL_ID})
    on_disk = json.loads(path.read_text())
    assert on_disk == {"X1": "up_x"}         # X2 was at the default, so it is not stored
    assert a.load()["X2"] == ALL_ID          # and still reads back as the default


# ── settings store ───────────────────────────────────────────────────────────

def test_toplist_settings_defaults_and_clamp(tmp_path: Path):
    s = ToplistSettings(tmp_path / "tl.json")
    assert set(s.load()) == set(FILTERABLE_LISTS)
    assert s.load()["rvol"]["rows"] == 25
    assert s.save({"rvol": {"rows": 9999}})["rvol"]["rows"] == 200
    assert s.save({"rvol": {"rows": 0}})["rvol"]["rows"] == 1
    with pytest.raises(ValueError):
        s.save({"nope": {"rows": 10}})


def test_every_list_has_a_label_and_a_scope():
    assert set(TOPLIST_LABEL) == set(FILTERABLE_LISTS) == set(FILTER_SCOPE)
    # The six ranked lists see the whole profile; the streams only the static half.
    assert all(FILTER_SCOPE[n] == "full" for n in TOPLISTS)
    assert all(FILTER_SCOPE[n] == "static" for n in PREMARKET_LISTS + STREAM_LISTS)


def test_member_predicate_is_static_only(tmp_path: Path):
    """A profile holding only dynamic conditions cannot scope a stream, so it
    reports scope 'none' rather than silently applying nothing.

    Saving one is rejected now, but a file written before that rule can still
    be on disk, so the guard has to stay. Built directly for that reason.
    """
    from scanner.profiles import CompiledProfile
    eng = ProfileEngine(ProfileStore(tmp_path / "profiles"),
                        SetupProfiles(tmp_path / "profiles.json"))
    legacy = CompiledProfile("up_dyn", "Legacy dynamic", "abc", static=[],
                             dynamic=[{"id": "rvol", "op": "gte", "value": 2.0,
                                       "option": "", "params": {}}])
    eng._compiled["up_dyn"] = legacy
    eng._assign[assignment_key("hod_lod")] = "up_dyn"
    keep, meta = member_predicate(eng, "hod_lod")
    assert keep is None and meta["scope"] == "none" and meta["id"] == "up_dyn"


def test_member_predicate_uses_the_member_set(tmp_path: Path):
    store = ProfileStore(tmp_path / "profiles")
    store.save({"id": "up_px", "name": "Price floor",
                "conditions": [{"id": "price", "op": "gte", "value": 15}]})
    assign = SetupProfiles(tmp_path / "profiles.json")
    assign.save({assignment_key("hod_lod"): "up_px"})
    eng = ProfileEngine(store, assign)

    cheap, rich = FakeState(symbol="CHEAP"), FakeState(symbol="RICH")
    cheap._last_close, rich._last_close = 3.0, 90.0
    eng.resolve_members({"CHEAP": cheap, "RICH": rich})

    keep, meta = member_predicate(eng, "hod_lod")
    assert keep is not None and meta["scope"] == "static"
    assert keep("RICH") and not keep("CHEAP")


def test_member_predicate_fails_open_when_unresolved(tmp_path: Path):
    """Opposite of the alert path on purpose: blanking a display list looks
    like a broken scanner, and nothing trades off these lists."""
    store = ProfileStore(tmp_path / "profiles")
    store.save({"id": "up_px2", "name": "Price floor",
                "conditions": [{"id": "price", "op": "gte", "value": 15}]})
    assign = SetupProfiles(tmp_path / "profiles.json")
    assign.save({assignment_key("hod_lod"): "up_px2"})
    eng = ProfileEngine(store, assign)          # members never resolved
    keep, meta = member_predicate(eng, "hod_lod")
    assert keep is None and meta["id"] == "up_px2"
