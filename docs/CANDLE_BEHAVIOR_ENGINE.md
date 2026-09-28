# Ordered candle-behavior engine

Status: **design note; not implemented**

This document captures a possible setup-level behavior engine for Edge Scanner NG.
It is deliberately separate from the current trigger combiner. Nothing described
here changes a live setup until an implementation is reviewed, replay-tested, and
explicitly enabled.

The companion [pattern inventory](CANDLE_BEHAVIOR_INVENTORY.md) records the
behavior vocabulary found in the sibling Z-Trading strategy documents and maps it
to current Edge capabilities.

## Problem

Edge has useful completed-candle triggers, including engulfing and harami bodies,
inside bars, long shadows, volume spikes, consecutive candles, range exits, VWAP
rejections, and failed swing breaks. A setup can combine trigger occurrences with
`or`, `and`, or `atleast` inside a time window.

That is not enough to describe an ordered behavior such as:

1. A bounded range forms.
2. A later candle closes above the frozen range high.
3. Volume expands and one or more later candles hold above that same level.
4. A close back inside the range or a failed retest cancels the pending setup.
5. Edge publishes one alert only after confirmation.

The existing setup combiner remembers which trigger instances occurred recently,
but it does not enforce order, share captured levels between triggers, represent a
pending confirmation state, or cancel a sequence through explicit invalidation.
The current `range_break` trigger combines range qualification, breakout, and
volume into one immediate event; it cannot wait for a later hold or retest.

## Ownership boundary

- **Edge** observes completed market data, advances detection state, and publishes
  a watch alert with evidence when a behavior is confirmed.
- **Trader** decides whether an Edge observation is relevant to a position or
  strategy and owns risk, entry, exit, protective stops, and orders.
- A behavior definition is a **setup detector**, not a trading strategy or order
  workflow.
- Edge publishes evidence and references to stored bars. It does not send bars to
  Trader.

## Proposed model

A behavior is an ordered, deterministic state machine owned by one setup. Each
setup/symbol/direction/session instance carries its own state and captured values.

```yaml
behavior:
  id: confirmed_range_breakout
  revision: 1
  timeframe_min: 5
  direction: long

  stages:
    - id: consolidation
      require:
        all: [range_consolidation]
      capture:
        range_high: pattern.range_high
        range_low: pattern.range_low

    - id: breakout
      after: consolidation
      within_bars: 3
      require:
        all: [breakout_close]
      compare:
        close_above: range_high
      capture:
        breakout_bar_id: source_bar.id

    - id: confirmation
      after: breakout
      within_bars: 2
      require:
        all:
          - volume_expansion
          - hold_above_breakout_level

  invalidate:
    any:
      - close_back_inside_range
      - failed_retest

  emit_on: confirmation
  expire_after_bars: 8
  rearm_on: new_consolidation
```

This is a design sketch, not a committed schema. The implementation ticket should
freeze names and validation rules before runtime work begins.

## Lifecycle

```text
idle
  -> consolidation_captured
  -> breakout_pending_confirmation
  -> confirmed -> alert emitted -> complete
                       |
                       +-> rearm only under the configured rule

Any nonterminal state
  -> invalidated
  -> expired
  -> unavailable
```

`invalidated` means a defined market event disproved the pending behavior.
`expired` means the next required stage did not arrive in time. `unavailable`
means Edge cannot evaluate safely because required inputs, continuity, or readiness
are missing. These states must not be collapsed into “did not fire.”

## Required runtime semantics

### Completed candles only

- The completed one-minute bar remains Edge's input clock.
- Structural behavior stages use completed, contiguous five-minute candles by
  default. A definition may explicitly select another supported timeframe.
- An in-progress wick cannot advance or invalidate a completed-candle behavior.
- Missing constituent minutes fail closed. A later candle cannot bridge a gap.

### No lookahead or self-inclusion

- A range, channel, average, or compression baseline must exclude the candle being
  evaluated as the breakout or confirmation candle.
- A candle cannot both create the historical range and break that same range.
- Captured levels are immutable for the life of one behavior instance.
- Replay and live evaluation must consume bars in identical market-time order.

### Deterministic state

- The state key includes setup revision, symbol, direction, session date, and the
  relevant connection/data epoch.
- Duplicate source bars are idempotent.
- A restart may reconstruct state only from complete stored bars. If continuity
  cannot be proven, the behavior becomes unavailable rather than guessing.
- At most one durable transition is recorded for one behavior instance and source
  bar. If a design permits multiple internal calculations on a bar, their final
  transition must still be deterministic and auditable.

### Ordered stages and captured values

- `after` is strict ordering, not merely two events inside the same time window.
- `within_bars` or an equivalent market-time expiry is required for every pending
  stage.
- Later predicates may read only explicitly captured values and current completed
  inputs.
- Captures record value, source bar, market timestamp, and the primitive that
  produced them.

### Confirmation and invalidation

- Confirmation supports explicit `all`, `any`, or `atleast` semantics.
- Invalidation is evaluated while a behavior is pending and wins over confirmation
  when both become true on the same completed candle unless a definition states a
  different, validated precedence.
- An invalidation clears the pending instance and records the reason. It does not
  publish the positive setup alert.
- A separate failure setup may consume the invalidation event if the operator
  intentionally configures one.

## Primitive detector contract

The behavior engine should orchestrate small detector primitives instead of adding
one handwritten Python function per named setup. A primitive should declare:

- stable ID and human-readable name;
- required timeframe and session;
- required inputs and warmup;
- whether it is a state, edge event, capture, confirmation, or invalidator;
- parameters with units and valid ranges;
- outputs and captured fields;
- completed-candle boundary and re-arm rule;
- positive, negative, equality-boundary, gap, restart, and date-boundary fixtures.

Existing trigger implementations may be adapted only where their contracts match.
Names from another project are requirements vocabulary, not proof that a detector
already exists.

## Evidence published with an alert

A confirmed behavior alert should add structured evidence without changing Edge's
role:

```json
{
  "behavior": {
    "id": "confirmed_range_breakout",
    "revision": 1,
    "state": "confirmed",
    "timeframe": "5m",
    "instance_id": "...",
    "captured": {
      "range_high": 101.25,
      "range_low": 99.80,
      "breakout_bar_id": "..."
    },
    "transitions": [
      {"to": "consolidation_captured", "market_timestamp": "...", "source_bar_id": "..."},
      {"to": "breakout_pending_confirmation", "market_timestamp": "...", "source_bar_id": "..."},
      {"to": "confirmed", "market_timestamp": "...", "source_bar_id": "..."}
    ]
  }
}
```

The normal alert still carries setup identity, trigger evidence, source bar,
detector revision, market timestamp, and live/replay mode. Trader can retrieve any
additional bars from the shared data store using the evidence references.

## Configuration and UI expectations

- Behavior definitions should be versioned alongside saved setups.
- Config must show stages in order, timing windows, captured levels, invalidators,
  expiry, re-arm behavior, and the exact candle timeframe.
- Setup Check should show `idle`, the current pending stage, satisfied stages,
  remaining bars, captured values, invalidation, expiry, or unavailability.
- Descriptions should use plain language first, with exact thresholds immediately
  below it.
- A new behavior remains disabled until replay evidence has been reviewed.

## Replay and test requirements

At minimum, the first behavior implementation needs fixtures for:

- valid consolidation -> breakout -> hold confirmation;
- breakout without volume;
- volume expansion without a breakout close;
- breakout followed by close back inside the frozen range;
- successful and failed retests;
- confirmation on the last eligible candle and one candle too late;
- same-bar confirmation/invalidation precedence;
- duplicate bars, missing minutes, restart gaps, and session rollover;
- old-epoch bars after reconnect;
- live/replay parity and chart-marker alignment;
- range/channel calculations that exclude the evaluated candle;
- no alert before confirmation and exactly one alert after confirmation.

Thresholds must be replay-tuned across multiple symbols and sessions. A single
attractive chart is not sufficient evidence for a production default.

## What Z-Trading contributes

The sibling Z-Trading worktree was inspected on 2026-09-26 at base commit
`456d12d05cc075b22f41efe02e09cf43d01bdf40`. Its 17 Markdown strategy documents
validated successfully. The worktree was dirty outside the source strategy
documents, so this is an inventory of the observed files, not a claim about a
published release.

Useful concepts:

- consistent `required_patterns`, `confirmation`, and `invalidation` vocabulary;
- versioned Markdown strategy specifications and compiled catalog hashes;
- structured decision evidence;
- append-only workflow transition history.

Important limitations:

- `candle_behavior` is copied into the compiled catalog but no runtime interprets
  its ordering, captures, confirmation, or invalidation;
- the volatility-breakout evaluator is a stateless current-snapshot rule;
- its Donchian calculation includes the evaluated candle, while the evaluator asks
  the close to exceed that channel by 0.5 percent. For positive prices, the close
  cannot exceed the current candle's high, so that implementation must not be
  copied as breakout evidence;
- Z-Trading's workflow store records arbitrary state changes but does not supply
  the candle-level transition contract Edge needs.

Reuse the vocabulary and evidence ideas. Do not import Z-Trading's strategy runtime
into Edge or treat the declarations as implemented detectors.

## Deferred implementation slices

1. Freeze a versioned behavior schema and primitive contract.
2. Add schema validation and configuration round-trip tests without live execution.
3. Implement an in-memory per-setup/symbol/session state store with audit evidence.
4. Implement the minimum primitives for one confirmed long range breakout.
5. Add replay fixtures and prove live/replay parity with no lookahead.
6. Expose pending behavior state in Setup Check.
7. Add behavior evidence to the alert feed without sending bars.
8. Ship the first definition disabled, tune it in replay, then review promotion.

Automatic trade actions are explicitly outside these slices.
