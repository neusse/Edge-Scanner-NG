"""Public trigger contracts, especially catalog entries missed by older tests."""

from collections import deque
from pathlib import Path
from types import SimpleNamespace

import pytest

from scanner.trigger_catalog import BY_ID, CATALOG, EvalCtx, SymbolSeries, describe, evaluate


DAY = "2026-09-15"


def test_contract_matrix_has_an_explicit_row_and_lifetime_for_every_native_trigger():
    from scanner.trigger_catalog import EVENT_LIFETIMES

    native = {trigger.id for trigger in CATALOG if trigger.source == "native"}
    assert len(native) == 45
    assert set(EVENT_LIFETIMES) == native
    matrix = (Path(__file__).resolve().parents[1] / "docs" / "TRIGGER_CONTRACTS.md").read_text()
    for trigger_id in native:
        trigger = BY_ID[trigger_id]
        assert trigger.lifetime == EVENT_LIFETIMES[trigger_id]
        assert f"| `{trigger_id}` |" in matrix
        assert trigger.sessions
        assert describe(trigger_id, trigger.default_options[0] if trigger.default_options else "", {})


@pytest.mark.parametrize("trigger", [t for t in CATALOG if t.source == "native"], ids=lambda t: t.id)
def test_every_native_trigger_keeps_identity_evidence_and_setup_check_aligned(trigger, tmp_path, monkeypatch):
    from scanner.custom_setups import CustomEvaluator, CustomSetupStore
    from scanner.trigger_catalog import Fire, _IMPL, trigger_key

    option = trigger.default_options[0] if trigger.default_options else ""
    store = CustomSetupStore(tmp_path / "setups", defaults=tmp_path / "none.json")
    store.save({"id": "contract", "name": "Contract", "enabled": True,
                "direction": "all", "sessions": list(trigger.sessions),
                "triggers": [{"id": trigger.id, "options": [option] if option else []}]})
    ev = CustomEvaluator(store)
    expected_direction = (trigger.direction if trigger.direction in ("long", "short", "neutral")
                          else next((o.direction for o in trigger.options if o.key == option), None) or "long")
    monkeypatch.setitem(_IMPL, trigger.id,
                        lambda ctx, opt, params: Fire(expected_direction, 123.0, "contract evidence"))
    state = SimpleNamespace(symbol="X", prior_close=100.0)
    session = "rth" if "rth" in trigger.sessions else trigger.sessions[0]
    timestamp = "2026-09-15T10:00:00-04:00" if session == "rth" else "2026-09-15T08:00:00-04:00"
    bar = {"symbol": "X", "timestamp": timestamp, "open": 100.0, "high": 101.0,
           "low": 99.0, "close": 100.0, "volume": 100.0}
    alert = ev.on_bar(state, bar)[0]
    identity = trigger_key(trigger.id, option)
    assert alert["entry_trigger"] == identity
    assert alert["triggers_fired"] == [identity]
    assert alert["trigger_value"] == 123.0
    assert alert["trigger_note"] == "contract evidence"
    assert alert["trigger_evidence"][0]["trigger"] == identity
    assert alert["trigger_evidence"][0]["status"] == "fired_now"
    check = ev.check(ev.store.get("contract"), state)["triggers"][0]
    normalized = ev.store.get("contract")["triggers"][0]["params"]
    assert check["label"] == describe(trigger.id, option, normalized)
    assert check["lifetime"] == trigger.lifetime
    assert check["fired_last_bar"]
    assert check["note"]


@pytest.mark.parametrize("trigger", [t for t in CATALOG if t.source == "native"], ids=lambda t: t.id)
def test_every_native_trigger_obeys_catalog_session_gate(trigger, monkeypatch):
    from scanner.trigger_catalog import Fire, _IMPL

    monkeypatch.setitem(_IMPL, trigger.id, lambda ctx, opt, params: Fire("neutral", 1, "test"))
    option = trigger.default_options[0] if trigger.default_options else ""
    ctx = context()
    ctx.session = "post"
    assert evaluate(trigger.id, ctx, option, {}) is None
    ctx.session = "rth"
    assert evaluate(trigger.id, ctx, option, {}) is not None


@pytest.mark.parametrize("trigger", [t for t in CATALOG if t.source == "native" and t.params],
                         ids=lambda t: t.id)
def test_parameter_distinct_native_instances_do_not_share_once_memory(trigger, monkeypatch):
    from scanner.trigger_catalog import Fire, _IMPL

    monkeypatch.setitem(_IMPL, trigger.id,
                        lambda ctx, opt, params: Fire("neutral", 1, "test") if ctx.once("contract") else None)
    param = trigger.params[0]
    first = param.default
    second = next((choice for choice in param.choices if choice != first), None)
    if second is None:
        second = first + (param.step or 1)
        if param.max is not None and second > param.max:
            second = first - (param.step or 1)
    assert second != first
    ctx = context()
    option = trigger.default_options[0] if trigger.default_options else ""
    assert evaluate(trigger.id, ctx, option, {param.key: first}) is not None
    assert evaluate(trigger.id, ctx, option, {param.key: first}) is None
    assert evaluate(trigger.id, ctx, option, {param.key: second}) is not None


def test_session_rollover_resets_once_memory_but_replay_can_prime_it():
    series = SymbolSeries("X")
    bar = {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
    series.on_bar(bar, 600, DAY, None)
    assert series.mem == {}
    series.mem["event"] = True
    series.on_bar(bar, 601, DAY, None)
    assert series.mem["event"]
    series.on_bar(bar, 600, "2026-09-16", None)
    assert "event" not in series.mem


@pytest.mark.parametrize("trigger", [t for t in CATALOG if t.source == "native"], ids=lambda t: t.id)
def test_first_live_bar_after_replay_preserves_trigger_memory_until_next_date(trigger, tmp_path, monkeypatch):
    from scanner.custom_setups import CustomEvaluator, CustomSetupStore
    from scanner.trigger_catalog import Fire, _IMPL

    option = trigger.default_options[0] if trigger.default_options else ""
    store = CustomSetupStore(tmp_path / "setups", defaults=tmp_path / "none.json")
    store.save({"id": "replay", "name": "Replay", "enabled": True,
                "direction": "all", "sessions": ["rth"],
                "triggers": [{"id": trigger.id, "options": [option] if option else []}]})
    ev = CustomEvaluator(store)
    monkeypatch.setitem(_IMPL, trigger.id,
                        lambda ctx, opt, params: Fire("long", 1, "first") if ctx.once("replay-contract") else None)
    state = SimpleNamespace(symbol="X", prior_close=100.0)
    replay = {"symbol": "X", "timestamp": "2026-09-15T10:00:00-04:00",
              "open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
    ev.series("X").on_bar(replay, 600, DAY, None)
    ev.prime_bar(state, replay, "rth")
    live = {**replay, "timestamp": "2026-09-15T10:01:00-04:00"}
    assert ev.on_bar(state, live) == []
    ev.reset()
    next_day = {**replay, "timestamp": "2026-09-16T10:00:00-04:00"}
    assert len(ev.on_bar(state, next_day)) == 1


def test_overnight_rollover_does_not_report_yesterdays_last_candle_as_new():
    series = SymbolSeries("X")
    green = {"open": 100, "high": 102, "low": 99, "close": 101, "volume": 100}
    series.on_bar(green, 955, DAY, None)
    new = {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 100}
    session = series.on_bar(new, 570, "2026-09-16", None)
    ctx = EvalCtx(state=SimpleNamespace(symbol="X"), series=series, bar=new,
                  et_min=570, session=session, external=set())
    assert not series.completed[5]
    assert evaluate("bull_candle_close", ctx, "5", {}) is None


def test_double_inside_cannot_span_previous_trading_date():
    old = candle(100, 105, 95, 101, 0)
    old["key"] = ("2026-09-14", "rth", 0)
    middle = candle(100, 103, 97, 101, 1)
    current = candle(100, 102, 98, 101, 2)
    ctx = context([old, middle, current])
    assert evaluate("double_inside_bar", ctx, "5", {}) is None


def test_doji_equality_boundary_and_zero_range():
    exact = context([candle(100, 101, 99, 100.25, 0)])
    assert evaluate("doji", exact, "5", {"body_pct": 12.5}) is not None
    too_wide = context([candle(100, 101, 99, 100.26, 0)])
    assert evaluate("doji", too_wide, "5", {"body_pct": 12.5}) is None
    flat = context([candle(100, 100, 100, 100, 0)])
    assert evaluate("doji", flat, "5", {"body_pct": 12.5}) is None


def test_volume_spike_uses_completed_candle_and_prior_equal_threshold():
    base = [candle(100, 101, 99, 100, i) for i in range(2)]
    spike = candle(100, 101, 99, 100, 2)
    spike["volume"] = 200
    ctx = context(base + [spike])
    fire = evaluate("volume_spike", ctx, "5", {"lookback": 2, "ratio": 2})
    assert fire is not None and fire.direction == "neutral" and fire.value == 2
    ctx.series.completed[5] = False
    assert evaluate("volume_spike", ctx, "5", {"lookback": 2, "ratio": 2}) is None


def test_rs_vs_spy_crosses_once_and_rearms_after_falling_below():
    state = SimpleNamespace(symbol="X", mom_15m_pct=1.0)
    ctx = context(state=state)
    ctx.spy_mom_15m = 0.0
    first = evaluate("rs_spy", ctx, "strong", {"pct": 0.5})
    assert first is not None and first.direction == "long" and first.value == 1.0
    assert evaluate("rs_spy", ctx, "strong", {"pct": 0.5}) is None
    state.mom_15m_pct = 0.4
    assert evaluate("rs_spy", ctx, "strong", {"pct": 0.5}) is None
    state.mom_15m_pct = 0.5
    assert evaluate("rs_spy", ctx, "strong", {"pct": 0.5}) is not None


def test_gap_uses_session_open_once_even_if_later_bar_remains_above_threshold():
    ctx = context(state=SimpleNamespace(symbol="X", prior_close=100.0))
    ctx.series.m1[0] = {"open": 102.0, "session": "rth", "close": 102.0}
    first = evaluate("gap", ctx, "up", {"min_pct": 2})
    assert first is not None and first.direction == "long" and first.value == pytest.approx(2)
    assert evaluate("gap", ctx, "up", {"min_pct": 2}) is None
    ctx.session = "pre"
    assert evaluate("gap", ctx, "up", {"min_pct": 2}) is None


def test_extended_hod_includes_premarket_but_rth_hod_does_not():
    series = SymbolSeries("X")
    first = {"open": 100, "high": 101, "low": 99, "close": 100, "volume": 1}
    second = {**first, "high": 102}
    series.on_bar(first, 480, DAY, None)
    session = series.on_bar(second, 481, DAY, None)
    ctx = EvalCtx(state=SimpleNamespace(symbol="X"), series=series, bar=second,
                  et_min=481, session=session, external=set())
    fire = evaluate("hod_ext", ctx, "high", {})
    assert fire is not None and fire.direction == "long" and fire.value == 102
    assert evaluate("hod", ctx, "high", {}) is None


def test_vwap_v_does_not_borrow_yesterdays_approach_candles():
    older = []
    for i, close in enumerate([101.2, 101.0, 100.8, 100.6, 100.5]):
        item = candle(close, close + .05, close - .05, close, i)
        item["key"] = ("2026-09-14", "rth", i)
        older.append(item)
    touch = candle(100.1, 100.7, 99.95, 100.6, 5)
    ctx = context(older + [touch])
    assert evaluate("vwap_v", ctx, "support", {}) is None


def candle(open_, high, low, close, index):
    return {"key": (DAY, "rth", index), "open": open_, "high": high,
            "low": low, "close": close, "volume": 100, "vwap": 100.0,
            "session": "rth"}


def context(candles=(), *, bar=None, previous=100.0, completed=True,
            partial=True, state=None):
    series = SymbolSeries("X")
    series.session_date = DAY
    series.candles[5] = deque(candles, maxlen=128)
    series.completed[5] = completed
    series.partial[5] = candle(100, 101, 99, 100, len(candles)) if partial else None
    series.m1.append({"close": previous, "session": "rth"})
    bar = bar or {"open": 100.0, "high": 101.0, "low": 99.0,
                  "close": 100.0, "volume": 100.0}
    series.m1.append({**bar, "session": "rth"})
    return EvalCtx(state=state or SimpleNamespace(symbol="X", prior_close=100.0, atr_d1=2.0),
                   series=series, bar=bar, et_min=600, session="rth", external=set())


_PATTERNS = [
    ("bull_candle_close", [(100, 102, 99, 101)], [(101, 102, 99, 100)], "long"),
    ("bear_candle_close", [(101, 102, 99, 100)], [(100, 102, 99, 101)], "short"),
    ("bear_engulfing", [(100, 103, 99, 102), (103, 104, 98, 99)],
     [(100, 103, 99, 102), (103, 104, 100, 101)], "short"),
    ("bull_harami", [(102, 103, 97, 98), (99, 102, 98, 101)],
     [(102, 103, 97, 98), (97, 102, 96, 101)], "long"),
    ("bear_harami", [(98, 103, 97, 102), (101, 102, 98, 99)],
     [(98, 103, 97, 102), (103, 104, 98, 99)], "short"),
    ("inside_bar", [(100, 105, 95, 101), (100, 103, 97, 101)],
     [(100, 105, 95, 101), (100, 105, 97, 101)], "neutral"),
    ("double_inside_bar", [(100, 105, 95, 101), (100, 103, 97, 101), (100, 102, 98, 101)],
     [(100, 105, 95, 101), (100, 103, 97, 101), (100, 103, 98, 101)], "neutral"),
    ("upper_shadow", [(100, 104, 99, 101)], [(100, 102, 99, 101)], "short"),
    ("lower_shadow", [(101, 102, 97, 100)], [(101, 102, 99, 100)], "long"),
]


@pytest.mark.parametrize("trigger,good,bad,direction", _PATTERNS)
def test_completed_candle_pattern_positive_negative_and_incomplete(trigger, good, bad, direction):
    positive = context([candle(*values, i) for i, values in enumerate(good)])
    fire = evaluate(trigger, positive, "5", {})
    assert fire is not None and fire.direction == direction
    assert fire.value is not None and fire.note
    negative = context([candle(*values, i) for i, values in enumerate(bad)])
    assert evaluate(trigger, negative, "5", {}) is None
    developing = context([candle(*values, i) for i, values in enumerate(good)], completed=False)
    assert evaluate(trigger, developing, "5", {}) is None


@pytest.mark.parametrize("trigger,previous,bar,direction,level", [
    ("new_candle_low", 100, {"open": 99, "high": 100, "low": 97, "close": 99, "volume": 100}, "short", 98),
    ("break_recent_high", 100, {"open": 100, "high": 103, "low": 99, "close": 103, "volume": 100}, "long", 102),
    ("break_recent_low", 100, {"open": 100, "high": 101, "low": 97, "close": 97, "volume": 100}, "short", 98),
])
def test_recent_level_triggers_strict_break_and_once_per_candle(trigger, previous, bar, direction, level):
    prior = candle(100, 102, 98, 100, 0)
    ctx = context([prior], previous=previous, bar=bar)
    params = {"since": 1} if trigger == "new_candle_low" else {"lookback": 1}
    fire = evaluate(trigger, ctx, "5", params)
    assert fire is not None and fire.direction == direction and fire.value == level
    assert evaluate(trigger, ctx, "5", params) is None
    equal = dict(bar)
    equal["low" if direction == "short" else "high"] = level
    equal["close"] = level
    boundary = context([prior], previous=previous, bar=equal)
    assert evaluate(trigger, boundary, "5", params) is None


@pytest.mark.parametrize("trigger,current,direction", [
    ("reject_last_high", candle(102, 103, 99, 99, 1), "short"),
    ("reject_last_low", candle(98, 101, 97, 101, 1), "long"),
])
def test_swing_rejections_need_a_completed_return_inside(trigger, current, direction):
    prior = candle(100, 102, 98, 100, 0)
    ctx = context([prior, current])
    fire = evaluate(trigger, ctx, "5", {"lookback": 1})
    assert fire is not None and fire.direction == direction
    ctx.series.completed[5] = False
    assert evaluate(trigger, ctx, "5", {"lookback": 1}) is None


def test_orb_breakdown_first_strict_cross_and_daily_latch():
    opening = candle(100, 102, 98, 100, 0)
    bar = {"open": 99, "high": 100, "low": 97, "close": 97, "volume": 100}
    ctx = context([opening], previous=99, bar=bar)
    fire = evaluate("orb_breakdown", ctx, "5", {})
    assert fire is not None and fire.direction == "short" and fire.value == 98
    assert evaluate("orb_breakdown", ctx, "5", {}) is None
    equal = context([opening], previous=99, bar={**bar, "close": 98})
    assert evaluate("orb_breakdown", equal, "5", {}) is None


@pytest.mark.parametrize("trigger,opt,previous,current,direction", [
    ("pct_change", "up", 104, 105, "long"),
    ("pct_change", "down", 96, 95, "short"),
    ("momentum_burst", "up", 100, 101, "long"),
    ("momentum_burst", "down", 100, 99, "short"),
])
def test_change_and_burst_boundaries(trigger, opt, previous, current, direction):
    bar = {"open": previous if trigger == "momentum_burst" else current,
           "high": max(previous, current), "low": min(previous, current),
           "close": current, "volume": 100}
    ctx = context(previous=previous, bar=bar)
    fire = evaluate(trigger, ctx, opt, {})
    assert fire is not None and fire.direction == direction
    assert fire.value is not None
    if trigger == "pct_change":
        assert evaluate(trigger, context(previous=current, bar=bar), opt, {}) is None
    else:
        assert evaluate(trigger, context(previous=previous, bar={**bar, "high": current,
                                                                   "low": current}), opt, {}) is None
