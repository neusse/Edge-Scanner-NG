# Dynamic Schwab Discovery And Live-Universe Plan

## Status

Converted into the draft
[Schwab Dynamic Discovery and Live Universe v2.0 PRD](../prds/schwab-dynamic-universe-v2.0-prd.md).
No live scanner behavior changes are authorized by either document until the
specification is approved and implementation tickets are created.

## Outcome

Use Schwab's existing authenticated WebSocket for two distinct jobs:

1. discover market-wide candidates through `SCREENER_EQUITY`; and
2. maintain a bounded, changing set of `CHART_EQUITY` and
   `LEVELONE_EQUITIES` subscriptions for the stocks Edge can evaluate.

The dashboard will expose the Schwab screener as its own window. A screener row
is only a candidate. It becomes alert-ready after Edge admits it to the live
universe, subscribes it, fills the required same-session history, and marks its
state ready.

This work remains detection-only. It does not choose trades or place orders.

## Why This Is A Versioned Change

The approved [Screener and Watchlist Universe v1.0 PRD](../prds/screener-universe-v1.0-prd.md)
explicitly lists changing subscriptions during a live session as a non-goal.
The proposed work intentionally reopens that decision. It must therefore produce:

- a v2 product specification for live discovery and turnover;
- an architectural decision record for runtime subscription and symbol-state
  lifecycle; and
- a migration statement explaining that saved watchlists and next-start
  universe selection remain supported.

Existing issues do not block this feature:

- #20 scan profiles are not required. This work applies only to the current
  live day-scanner and does not introduce horizons or profiles.
- #21 moving next-start universe selection into Config is postponed. Existing
  watchlist and universe-selection controls remain where they are.
- #24 owns additional saved-screener metrics and remains related, but Schwab
  stream discovery does not depend on those metrics.
- #74 owns cache-only bar retrieval for downstream consumers, not live promotion.

These scope decisions keep dynamic discovery independent from the larger scan-
profile design and from rearranging the current universe-selection UI.

## Current State

- Edge opens one Schwab WebSocket.
- The running scanner currently resolves 256 stock states plus 12 support
  symbols, using 268 of the observed 300 `CHART_EQUITY` slots.
- `CHART_EQUITY` supplies Schwab one-minute OHLCV bars.
- `LEVELONE_EQUITIES` supplies quote/trade state and is a separate, larger
  subscription tier. Edge currently assumes 3,000 slots but already parses a
  Schwab limit response so the server remains authoritative.
- Edge can add and remove extra Level One quote watches for externally held
  symbols, but cannot rotate Chart Equity symbols or scanner states.
- Edge's RVOL, gainers, losers, and 5-minute movers rank only symbols already in
  the scanner. They do not discover outside symbols.
- Schwabdev exposes `SCREENER_EQUITY`, including volume, trades, percent-change
  up/down, and average-percent-volume lists for market/index prefixes and
  multiple time windows. Edge does not subscribe to or display it.
- Watchlists and Yahoo screening remain useful for deliberate next-session
  universe preparation.

## Domain Model For The Specification

The v2 specification should define these terms in `CONTEXT.md` if adopted:

**Discovery list**  
A provider-ranked list that can contain symbols outside Edge's live universe.
It supplies candidates, not alerts.

**Candidate**  
A symbol observed through discovery with source, list key, rank, first-seen,
last-seen, provider values, and eligibility state.

**Live universe**  
The symbols whose market data and warmed state Edge currently maintains for
setup evaluation. It includes fixed support symbols and eligible stocks.

**Pinned symbol**  
A symbol that automatic turnover cannot remove. Initial reasons include market
and sector references, manual pins, open positions, trade watches, and an
explicit operator hold.

**Subscription lease**  
A live-universe symbol's bounded claim on provider capacity, including admission
reason, acquisition time, minimum residence time, priority, and provider
subscription acknowledgement.

**Readiness**  
The explicit state describing whether a promoted symbol has enough daily,
same-session, indicator, sector, and quote context to evaluate setups honestly.

## Proposed Module Seams

### 1. Schwab discovery adapter

Own all `SCREENER_EQUITY` protocol details behind a small interface:

```text
configure(list_keys)
on_snapshot(callback)
status()
```

Its implementation shares the already-open Schwab stream, parses provider
payloads into normalized discovery snapshots, records acknowledgements and
errors, and never opens another Schwab connection.

Initial long-oriented list keys should be specified explicitly. Likely starting
points are percent-change-up, volume, trades, and average-percent-volume over
1-, 5-, 10-, and all-day windows. Percent-change-down can remain available for
inspection without becoming a promotion signal.

### 2. Subscription controller

Own provider capacity and runtime `ADD`/`UNSUBS` behavior behind a reconciliation
interface:

```text
reconcile(desired_chart_symbols, desired_quote_symbols) -> result
status() -> acknowledged membership, limits, errors, epochs
```

The implementation computes a diff, sends commands on the existing stream,
waits for provider acknowledgement, and updates membership only from confirmed
results. It must handle partial acceptance and Schwab code-19 limits without
silently dropping requested symbols.

### 3. Candidate registry

Merge screener snapshots into one session-scoped record per symbol. Preserve:

- every discovery source and current rank;
- first-seen, last-seen, and recurrence;
- provider price, change, volume, and trade values;
- eligibility failures;
- promotion/demotion history; and
- whether the symbol is already live, warming, ready, pinned, cooling down, or
  unavailable.

Persist bounded final/session snapshots for after-hours watchlist review. Do not
treat provider discovery values as Edge indicators or alerts.

### 4. Long-only live-universe allocator

Produce a desired live universe from fixed symbols, pinned symbols, the current
core universe, and eligible candidates. Keep policy separate from Schwab
protocol handling.

The first policy should be conservative and explainable:

- fill unused Chart Equity capacity before evicting anything;
- reserve capacity for SPY, sector ETFs, held positions, and operational safety;
- require price, liquidity, spread, security-type, and long-direction checks;
- rank recurring positive candidates above one-off appearances;
- enforce minimum residence and promotion cooldown periods;
- use hysteresis so the entry threshold is higher than the retention threshold;
- demote sustained low priority, not a single LOD, red candle, or losing alert;
- never evict a pinned, warming, held, or actively watched symbol; and
- make every admission and eviction reason visible.

Policy values belong in versioned configuration with safe defaults. Automatic
turnover must initially be disabled until observed in shadow mode.

### 5. Dynamic symbol lifecycle

Promotion must be an ordered state machine:

```text
candidate
  -> admitted
  -> stream subscription requested
  -> buffering live bars
  -> daily/session history loaded
  -> buffered and historical bars merged and deduplicated
  -> indicators and setup state warmed
  -> ready
  -> setup evaluation enabled
```

A promoted symbol must not publish alerts before `ready`. Subscribe before the
bounded history load so bars arriving during warmup are buffered rather than
lost. One incremental history request at promotion is acceptable; repeated
polling is not. If history is unavailable, the symbol remains visible as not
ready or can be rejected according to the specification.

Demotion must disable new setup evaluation, confirm unsubscribe results, retain
session evidence for inspection, and release the lease. Re-promotion must not
inherit stale latches, incomplete candles, or another subscription epoch.

## Schwab Market Screener Window

Add a dashboard window distinct from Edge's 5-Min Movers window.

Required content:

- selectable Schwab screener keys grouped by market, measure, and period;
- latest provider timestamp and stream-health state;
- symbol, description, rank, last price, net change, percent change, volume,
  trades, market share, first seen, and last seen;
- badges for outside universe, candidate, warming, ready, pinned, rejected,
  cooling down, and unavailable;
- discovery reason and eligibility explanation;
- current Chart and Level One budget usage;
- actions to inspect/link a symbol, pin it, request promotion, release it, and
  save selected candidates to a named/described watchlist; and
- a clear label that screener values are provider discovery data, not an Edge
  setup or alert.

The window should allow manual promotion before automatic turnover is enabled.
It should preserve the final bounded snapshot for after-hours review and
watchlist maintenance.

## Configuration And Operating Modes

The specification should define three stages:

1. **Observe**: display and retain Schwab screener data; do not alter subscriptions.
2. **Manual**: an operator promotes/releases candidates; all readiness rules apply.
3. **Automatic**: the allocator may reconcile subscriptions within configured
   budgets and protection rules.

Durable dynamic-discovery settings should expose:

- enabled screener list keys;
- reserved dynamic Chart slots;
- maximum promotions per interval;
- minimum residence and cooldown durations;
- eligibility and priority policy;
- pin/protection rules;
- shadow-versus-active turnover; and
- persistence/retention settings.

The live window is operational control. Durable policy may use a focused
Schwab-discovery settings section; this work does not move the existing scanner-
universe selector or implement #21.

## Reconciliation Rules

- There is exactly one Schwab WebSocket per account/process.
- Screener, Chart Equity, and Level One are separate services on that connection.
- The server's acknowledged membership and limit responses are authoritative.
- A screener appearance does not imply Chart or Level One membership.
- Chart and Level One membership must be tracked separately.
- No symbol may enter setup evaluation without complete readiness evidence.
- Provider reconnect must restore desired subscriptions, obtain fresh
  acknowledgements, assign a new epoch, and revalidate readiness.
- Missing acknowledgements or ambiguous membership fail closed for alerts.
- Replay never connects to discovery or performs live turnover. Future replay of
  this feature requires recorded discovery snapshots and subscription events.

## Observability And Evidence

Expose and retain:

- requested versus acknowledged subscriptions per service;
- capacity used, reserved, available, and rejected;
- screener update age and connection epoch;
- candidate counts by state;
- promotion/demotion decisions and policy evidence;
- warmup duration, buffered bars, merge gaps, and readiness failures;
- reconnect/restoration results; and
- a session audit log that can explain why a symbol was or was not monitored.

These values should be available in the dashboard and structured logs. Avoid a
large collection of protocol-specific endpoints; expose normalized status from
the responsible modules.

## Safety And Failure Cases

- Provider rejects an `ADD`, partially accepts a chunk, or reports a lower cap.
- The candidate has no usable daily or same-session history.
- A symbol changes state while its history is loading.
- A symbol is selected for eviction while a position or trade watch appears.
- A reconnect occurs during promotion or demotion.
- A late bar from an old subscription epoch arrives after demotion.
- Screener updates freeze while Chart/Level One remain healthy, or vice versa.
- Several screener lists nominate the same symbol.
- A symbol repeatedly crosses admission and retention thresholds.
- After-hours review sees the final session snapshot without presenting it as live.
- External watchlist edits occur while the live universe is changing.

Each case needs a specified fail-closed result and a test fixture.

## Overall Acceptance Criteria

- A Schwab Market Screener window displays normalized `SCREENER_EQUITY` updates
  from the scanner's existing connection.
- Its rows may include symbols outside the current live universe.
- Edge's 5-Min Movers remains explicitly scoped to the existing live universe.
- Manual promotion can add an eligible candidate without restarting or opening
  another Schwab stream.
- Manual release can remove an unprotected dynamic symbol without interrupting
  other subscriptions.
- Requested and acknowledged Chart/Level One memberships remain separately visible.
- A promoted symbol cannot emit an alert until its historical and live state is
  merged, complete, and ready.
- A reconnect safely restores desired subscriptions and readiness.
- Automatic turnover operates in shadow mode before it can change subscriptions.
- Automatic turnover never removes protected symbols and does not react to one
  negative alert alone.
- Final screener candidates can be reviewed after hours and saved to existing
  named/described watchlists.
- Existing watchlists, next-start universe selection, Yahoo screener behavior,
  alert contracts, and replay isolation remain compatible.

## Specification And Ticket Plan

Create one GitHub map issue for the program, then link the following child
tickets. Do not create implementation tickets until the specification and ADR
resolve the questions in the first two tickets.

### Ticket 1 — Specify Schwab discovery and dynamic live-universe behavior

Deliver `docs/prds/schwab-dynamic-universe-v2.0-prd.md`.

Resolve user workflow, operating modes, initial screener keys, candidate fields,
pin reasons, manual controls, automatic-policy inputs, after-hours retention,
compatibility with existing watchlists and #24, and end-to-end acceptance
criteria. Explicitly exclude scan profiles (#20) and moving universe selection
into Config (#21).

### Ticket 2 — Record the subscription and symbol-lifecycle architecture

Deliver an ADR defining the single-stream invariant, module seams, requested
versus acknowledged state, capacity handling, reconnect behavior, subscription
epochs, promotion/demotion state machines, and alert-readiness gate.

Blocked by Ticket 1.

### Ticket 3 — Add Schwab Screener Equity adapter and normalized fixtures

Implement `SCREENER_EQUITY` subscription configuration, payload normalization,
health/status reporting, and fixture-driven tests. Do not change Chart or Level
One membership.

Blocked by Tickets 1 and 2.

### Ticket 4 — Add the Schwab Market Screener dashboard window

Display live normalized rows, list selection, health, timestamps, discovery
provenance, and linked-symbol behavior. Start in Observe mode only.

Blocked by Ticket 3.

### Ticket 5 — Persist bounded session candidate snapshots

Implement the candidate registry, session rollover, bounded persistence,
after-hours review, and save-to-watchlist flow with provenance.

Blocked by Ticket 3. Can proceed in parallel with Ticket 4 after that dependency.

### Ticket 6 — Implement acknowledged runtime subscription reconciliation

Add Chart and Level One `ADD`/`UNSUBS` reconciliation, separate membership,
server-limit handling, reconnect restoration, epochs, audit events, and tests.
Do not connect it to automatic candidate selection.

Blocked by Ticket 2.

### Ticket 7 — Implement dynamic symbol warmup and readiness gating

Add live buffering, bounded promotion backfill, bar merge/deduplication, dynamic
state creation, setup-plan participation, demotion cleanup, and fail-closed alert
gating.

Blocked by Ticket 6.

### Ticket 8 — Add manual promotion, release, and protection controls

Connect the window to the subscription and readiness modules. Add manual pins,
protected reasons, capacity preview, confirmation/status feedback, and failure
recovery. Manual mode is the first production-changing release.

Blocked by Tickets 4, 5, and 7.

### Ticket 9 — Implement long-only allocator in shadow mode

Implement deterministic eligibility, priority, reserved capacity, hysteresis,
minimum residence, cooldown, protection, and decision evidence. Compare its
desired membership with the real manual membership without sending commands.

Blocked by Tickets 5 and 7.

### Ticket 10 — Enable guarded automatic turnover

Allow an explicit Config setting to apply the shadow-tested desired membership.
Add rate limits, emergency disable, restart/reconnect recovery, dashboard audit,
and a safe upgrade default of disabled.

Blocked by Tickets 8 and 9.

### Ticket 11 — Complete integration, replay, operations, and documentation

Test one-stream behavior, capacity errors, reconnects, warmup races, alert
readiness, protected-symbol changes, persistence, and upgrade compatibility.
Document the new window, Config policy, troubleshooting, after-hours workflow,
and the distinction among discovery candidates, live-universe symbols, setups,
and alerts. Record dynamic discovery as unavailable in replay until recorded
discovery inputs exist.

Blocked by all implementation tickets.

## Recommended Delivery Milestones

1. **Specification complete**: Tickets 1–2.
2. **Visible discovery**: Tickets 3–5; no subscription turnover.
3. **Safe manual turnover**: Tickets 6–8.
4. **Measured automation**: Ticket 9 in shadow mode for multiple sessions.
5. **Guarded automation and release**: Tickets 10–11 after reviewing shadow evidence.

## Open Decisions For The Specification

- Which market prefixes and screener keys are enabled by default?
- How many of the 300 observed Chart slots are fixed, reserved, and dynamic?
- Which source proves an open position or active trade watch for pinning?
- What minimum same-session history does each enabled setup require?
- Is one promotion REST backfill preferred, or should some symbols wait for
  enough streamed history?
- Which eligibility checks are mandatory before promotion versus before alerts?
- What residence time, cooldown, and churn rate are acceptable?
- Does a manually released symbol remain in the candidate window and for how long?
- How much candidate and decision history is retained after the session?
- Which evidence is added to alerts for dynamically admitted symbols without
  breaking the versioned alert contract?

## Ticket-Creation Procedure

1. Review and approve this plan.
2. Create a GitHub issue labelled `wayfinder:map` for the program.
3. Create Tickets 1 and 2 first, link them as sub-issues, and mark the ADR blocked
   by the product specification.
4. After those documents are approved, create implementation Tickets 3–11 with
   the dependency chain above and `ready-for-agent` only where the interface and
   acceptance criteria are settled.
5. Reference #24 where saved-screener metrics overlap. Record #20 as unnecessary
   for this feature and #21 as postponed; do not make either a dependency.
6. Close the map only after documentation and operational evidence are complete,
   not merely when automatic turnover first runs.
