# Schwab Dynamic Discovery And Live Universe - Product Requirements Document

## Summary

- **Problem:** Edge's live rankings can only rank symbols already in the scanner.
  Schwab can discover market-wide leaders through `SCREENER_EQUITY`, while the
  running scanner can change `CHART_EQUITY` and `LEVELONE_EQUITIES`
  subscriptions without opening another WebSocket. Edge does not currently
  expose that discovery feed or safely admit discovered symbols into setup
  evaluation.
- **Target users:** A local, long-only day trader using Edge with Schwab market
  data who wants the scanner to react to changing market leadership during the
  session.
- **Proposed solution:** Add a Schwab Market Screener window, a session-scoped
  candidate registry, and acknowledged runtime subscription management. Deliver
  the feature in Observe, Manual, Shadow, and Automatic modes, with explicit
  symbol warmup and readiness before alerts are allowed.
- **Version:** 2.0
- **Status:** Draft specification for approval

## Product Decisions

- Edge continues to use exactly one Schwab WebSocket.
- `SCREENER_EQUITY` is discovery. Its rows are candidates, not Edge setups or
  alerts.
- `CHART_EQUITY` provides the one-minute OHLCV bars used for scanner state.
- `LEVELONE_EQUITIES` provides quote/trade state and is tracked separately from
  Chart Equity membership.
- The server's acknowledgements and limit responses are authoritative. The
  observed Chart Equity capacity is 300, but the implementation must not treat
  that observation as a permanent contract.
- The startup universe becomes the initial live universe for that session. It
  is not automatically pinned merely because it came from the startup universe.
- Dynamic admissions and removals are a session-only overlay. They do not
  rewrite the selected startup watchlist or next-start universe.
- Existing watchlist and universe-selection controls remain in place. Issue #21
  is postponed and is not part of this feature.
- Scan profiles and day-versus-swing behavior are not required. Issue #20 is not
  part of this feature.
- Automatic turnover ships disabled. Observe mode is the upgrade default;
  Manual mode is delivered before any Automatic mode can be enabled.
- A single LOD, red candle, failed setup, or losing alert cannot by itself evict
  a symbol.
- Replay never connects to Schwab discovery or changes live subscriptions.

## Goals

- Display normalized Schwab `SCREENER_EQUITY` results in a dedicated dashboard
  window, including symbols outside Edge's live universe.
- Keep Edge's existing 5-Min Movers and other toplists explicitly scoped to the
  live universe.
- Add and remove Chart Equity and Level One subscriptions at runtime without
  interrupting the Schwab connection.
- Make requested and acknowledged membership, provider capacity, and failures
  visible.
- Warm dynamically admitted symbols from legitimate historical and live data
  before enabling setup evaluation.
- Support manual admission and release before guarded automatic turnover.
- Retain bounded session discovery and decision evidence for after-hours review
  and watchlist maintenance.
- Preserve Edge's role as detector: discovery and live-universe membership do
  not become trade decisions or orders.

## Non-Goals

- Opening a second Schwab stream.
- Streaming the entire equity market through Chart Equity or Level One.
- Replacing the existing Yahoo screener or saved-watchlist workflow.
- Moving startup-universe selection into Config (#21).
- Implementing scan profiles, swing scanning, or alert-horizon segregation (#20).
- Treating a screener row as an alert or sending it directly to Trader.
- Automatically placing orders, selecting strategies, or managing positions.
- Using Alpaca IEX data to fill Schwab live bars or quote gaps.
- Changing the versioned alert-feed contract without a separate documented
  contract update.
- Replaying live discovery before recorded discovery and subscription inputs
  exist.

## Definitions

The following terms must be added to `CONTEXT.md` when implementation begins.

**Discovery list**  
A provider-ranked list that may include symbols outside Edge's live universe.

**Candidate**  
A symbol observed in one or more discovery lists, with discovery provenance and
an eligibility state. A candidate is not an alert.

**Live universe**  
The symbols for which Edge currently maintains market-data subscriptions and
warmed scanner state for setup evaluation, plus required support symbols.

**Pinned symbol**  
A live-universe symbol that automatic turnover cannot remove while at least one
pin reason remains active.

**Subscription lease**  
A symbol's session-scoped claim on provider capacity, including admission
reason, acquisition time, residence rule, priority, and acknowledged services.

**Readiness**  
The explicit evidence that a symbol has sufficient daily, intraday, session,
sector, quote, and indicator state for its enabled setups to evaluate honestly.

## Requirements

### Schwab Stream Requirements

- Use the `SCREENER_EQUITY`, `CHART_EQUITY`, and `LEVELONE_EQUITIES` services on
  the scanner's existing authenticated Schwab WebSocket.
- Subscribe, add, and unsubscribe without logging out or restarting the stream.
- Track requested and acknowledged membership independently for every service.
- Do not mark an `ADD` or `UNSUBS` successful merely because it was sent.
- Parse Schwab limit responses, including partial acceptance, and expose the
  provider-reported capacity and discarded symbols.
- Preserve a monotonically changing connection/subscription epoch so late
  events from an earlier membership can be rejected.
- On reconnect, restore the desired membership, obtain new acknowledgements,
  create a new epoch, and revalidate readiness before alerts resume.
- A frozen Screener service must not be reported as healthy merely because
  Chart Equity or Level One continues updating, and vice versa.

### Initial Discovery Lists

The first release must support the full documented key builder but enable this
long-oriented starter set by default:

- `EQUITY_ALL_PERCENT_CHANGE_UP_1`
- `EQUITY_ALL_PERCENT_CHANGE_UP_5`
- `EQUITY_ALL_PERCENT_CHANGE_UP_10`
- `EQUITY_ALL_PERCENT_CHANGE_UP_60`
- `EQUITY_ALL_VOLUME_5`
- `EQUITY_ALL_TRADES_5`
- `EQUITY_ALL_AVERAGE_PERCENT_VOLUME_5`

The UI may enable equivalent NYSE, NASDAQ, index, or all-day keys. Percent-
change-down lists may be viewed but are not positive promotion evidence in the
long-only allocator.

The implementation must validate keys against supported:

- market/index prefixes;
- sort fields; and
- periods of all day, 1, 5, 10, 30, or 60 minutes.

An invalid or provider-rejected key remains visible with its error and does not
disable valid lists.

### Candidate Registry Requirements

- Merge repeated appearances into one session record per symbol.
- Preserve every contributing list key and its current and best rank.
- Record first seen, last seen, recurrence count, provider timestamp, last
  price, net change, percent change, volume, trades, market share, description,
  and raw-source provenance where supplied.
- Record eligibility results, current live-universe state, pin reasons,
  admission/release history, readiness, cooldown, and provider errors.
- Keep missing values distinct from zero.
- Reset live candidate state at the exchange-session boundary while retaining a
  bounded immutable session record for after-hours review.
- Retain the most recent 20 trading-session summaries by default. Retention is
  configurable and bounded.
- Saving candidates to a watchlist must use the existing named/described
  watchlist store and add discovery source/capture metadata without changing the
  active live universe.

### Candidate Eligibility Requirements

A candidate may be considered for manual or automatic admission only when:

- the symbol is a valid supported U.S. equity or ETF symbol;
- the discovery update is from the current connection epoch and is not stale;
- its last price is positive and finite;
- its percent change is positive for automatic long-only admission;
- it is not halted or in an unsupported security state when that state is
  available;
- a current Level One quote can be obtained;
- its bid and ask are valid, unlocked, and within the configured maximum spread;
- required daily and intraday history can be obtained from Edge's Schwab cache
  or bounded Schwab history acquisition; and
- provider capacity is available or an eligible unprotected lease can be
  released.

Price, liquidity, spread, and security-type thresholds must be versioned
settings. Their exact values are operator policy, not hidden constants. Manual
admission may override a soft ranking threshold, but cannot override invalid
data, provider capacity, unsupported security state, or readiness requirements.

### Live-Universe Capacity Requirements

- Compute capacity from provider acknowledgement and the most recent explicit
  provider limit. Fall back to the currently observed Chart Equity limit of 300
  only until Schwab reports otherwise.
- Track Chart Equity and Level One capacity separately.
- Reserve all required support symbols before allocating stock leases.
- Maintain five unused Chart Equity slots by default as operational headroom.
- Fill unused non-reserved capacity before considering an eviction.
- Show fixed, pinned, warming, ready, available, reserved, requested, rejected,
  and total slots in the dashboard.
- Never silently truncate a desired membership list.
- If capacity shrinks below protected membership, preserve protected symbols,
  stop new admissions, report a blocking degraded state, and require operator
  action rather than guessing which protected symbol to remove.

### Pin And Protection Requirements

Automatic release is prohibited while any of these reasons applies:

- `support`: SPY or a required sector/reference symbol;
- `manual`: explicitly pinned by the operator;
- `external_watch`: present in the existing external quote-watch set;
- `warming`: admission is still merging and validating data;
- `minimum_residence`: the lease has not completed its minimum residence; or
- `operator_hold`: an explicit temporary session hold.

Edge must not claim that a symbol represents an open position unless an
authorized external owner explicitly supplies that state. Existing external
quote watches are protected, but are labeled as watches rather than inferred
positions.

Pin reasons are additive. Removing one reason does not unpin the symbol while
another remains.

### Promotion And Readiness Requirements

Promotion follows this state machine:

```text
candidate
  -> admitted
  -> subscriptions requested
  -> subscriptions acknowledged
  -> live bars buffered
  -> cached history inspected
  -> missing history acquired once through the shared rate limiter
  -> historical and buffered bars merged and deduplicated
  -> indicators and setup state warmed
  -> ready
  -> setup evaluation enabled
```

- Subscribe before history acquisition so live bars are buffered during warmup.
- Inspect existing daily, intraday-profile, and current-session cache coverage
  before requesting data.
- Acquire only missing intervals. Promotion must not trigger periodic REST
  polling.
- Route every required request through Edge's shared Schwab rate limiter.
- Prefer the current-epoch streamed bar when a streamed and historical bar have
  the same timestamp; record material OHLCV disagreement.
- Do not evaluate or emit setups for the symbol before `ready`.
- Readiness is evaluated against the enabled setup plan. A setup whose minimum
  history is unavailable remains disabled for that symbol with an explicit
  reason.
- When baseline cache coverage is complete, the target is readiness within 30
  seconds of acknowledged subscriptions. Provider-limited history acquisition
  has no hard latency promise but must expose progress and remain bounded.
- If warmup fails, release the Chart lease unless manually held, retain the
  candidate and failure reason, and apply a retry cooldown.

### Release Requirements

- Disable new setup evaluation before requesting unsubscribe.
- Reject late events from the released subscription epoch.
- Confirm Chart and Level One removal separately.
- Retain the symbol's session state and audit evidence for inspection; remove it
  from active rankings after acknowledged release.
- Do not release a protected symbol.
- Re-admission creates a new lease and epoch and must not inherit stale trigger
  latches, incomplete candles, or prior readiness.

### Long-Only Allocation Requirements

The allocator produces a desired live universe; it does not send provider
commands itself.

- Positive evidence may include discovery-list rank, recurrence across lists,
  improvement in rank, positive percent change, volume/trade participation,
  current spread quality, and live positive momentum once subscribed.
- A percent-change-down list, LOD, negative momentum, or failure to recur may
  lower retention priority.
- No single negative observation may force release.
- Admission uses a higher score threshold than retention to provide hysteresis.
- The default minimum residence is 30 minutes after readiness.
- The default release cooldown is 15 minutes before the same symbol may be
  automatically readmitted.
- Automatic reconciliation is limited to two admissions and two releases per
  five-minute interval by default.
- Automatic turnover is disabled during reconnect recovery, provider degraded
  states, replay, and outside the configured live-session interval.
- Automatic turnover first runs in Shadow mode for at least five complete
  trading sessions. Shadow decisions are retained and reviewed before Automatic
  mode can be enabled.
- Initial scoring weights and thresholds must be versioned and visible. They may
  be tuned from shadow evidence without changing the provider protocol module.

### Operating Modes

1. **Observe**: subscribe to and display Screener Equity; never alter Chart or
   Level One membership. This is the upgrade default.
2. **Manual**: the operator may admit, pin, hold, and release symbols. Readiness
   and protection rules remain mandatory.
3. **Shadow**: compute and record automatic decisions without changing provider
   membership. Manual controls remain available.
4. **Automatic**: apply the allocator's desired membership through the
   subscription controller, within rate, protection, and health rules.

Mode changes are explicit, session-audited, and fail closed. Restart preserves
durable configuration but returns to the configured safe mode; a software
upgrade must never silently enable Automatic mode.

### Schwab Market Screener Window

Add a dashboard window named **Schwab Market Screener**, distinct from
**5-Min Movers**.

The window must provide:

- market, ranking measure, and period selection;
- combined and per-list views;
- stream health, connection epoch, provider timestamp, and receipt age;
- symbol, description, rank, best rank, price, net change, percent change,
  volume, trades, market share, first seen, last seen, and recurrence;
- state badges for outside universe, candidate, requested, warming, ready,
  pinned, held, cooling down, rejected, released, and unavailable;
- discovery and eligibility explanations in plain language;
- Chart and Level One requested/acknowledged membership and capacity;
- linked-symbol behavior compatible with existing chart, news, and stock-info
  windows;
- manual Admit, Release, Pin/Unpin, Hold/Unhold, and Save to watchlist actions;
- a clear distinction between provider discovery values and Edge indicators;
  and
- access to the final retained session snapshot after the live feed stops.

Destructive or rejected actions must leave the current live membership intact
and return a visible reason.

### Configuration Requirements

Add focused Schwab-discovery settings without moving the existing startup-
universe selector:

- operating mode;
- enabled screener keys;
- Chart headroom;
- promotion/release rate limits;
- minimum residence and readmission cooldown;
- eligibility and scoring settings;
- maximum spread and freshness;
- session interval for automatic activity;
- retention duration; and
- automatic emergency disable.

The live window owns immediate session controls. Durable policy settings remain
separate from ordinary watchlist editing.

### Data And Audit Requirements

Persist sufficient structured evidence to answer:

- Why did a symbol appear?
- Why was it eligible or rejected?
- Why was it admitted, retained, or released?
- Which services acknowledged it and under which epoch?
- Which history was reused or acquired?
- When did it become alert-ready?
- Which protection reasons applied?
- What would Shadow or Automatic mode have changed?

Persist normalized data, not unbounded raw WebSocket traffic. Records must be
written atomically or append-safely and recover from a truncated final write.
Credentials, account identifiers, and tokens must never enter discovery or
audit files.

### Integration Requirements

- Reuse the existing Schwabdev stream object and receiver.
- Reuse the shared Schwab REST rate limiter and provider-specific caches.
- Reuse `SymbolState`, `SymbolSeries`, indicator, setup-plan, profile, sector,
  and chart paths after a symbol is ready.
- Reuse the existing watchlist store and source metadata for saved candidates.
- Reuse the dashboard window registry, linked-symbol behavior, virtual table,
  and styling.
- Preserve the current alert feed. If dynamic membership evidence is later
  required in an alert, change the versioned alert contract separately.
- Preserve replay's mutual exclusion from the live scanner.
- Keep Trader separate: no bars, screener candidates, or automatic membership
  decisions are sent to Trader as trade instructions.

### Constraints

- **Performance:** Screener payload processing must not block bar ingestion.
  Candidate merging and allocation run outside the bar callback's critical
  path. The UI should reflect a received screener update within two seconds.
- **Compatibility:** Existing watchlists, universe selection, Yahoo screening,
  saved setup configuration, alerts, charts, and current startup remain valid.
- **Security:** Use the existing Schwab session and token path. Store no secrets
  or account details in candidate records.
- **Operations:** Never open a second Schwab stream. Never restart the scanner to
  perform a promotion. Fail closed when membership or readiness is ambiguous.
- **Provider truth:** Treat documented/observed capacities as hints until the
  server acknowledges membership or reports a limit.

## User Workflows

### Observe Schwab discovery

1. The scanner starts its existing Schwab stream.
2. Edge subscribes to the configured Screener Equity keys on that connection.
3. The user adds a Schwab Market Screener window.
4. Ranked symbols appear whether or not they are in the live universe.
5. The user links a row to charts/news/stock information or saves it to a
   watchlist.
6. No Chart or Level One membership changes in Observe mode.

### Manually admit a candidate

1. The user selects an eligible outside-universe candidate and chooses Admit.
2. Edge previews capacity and any required eviction. Manual admission cannot
   evict a protected symbol.
3. Edge requests Chart and Level One membership and shows acknowledgement state.
4. Live bars buffer while Edge inspects caches and obtains only missing history.
5. The window shows warmup/readiness progress.
6. Once ready, the symbol enters Edge rankings and setup evaluation.
7. If any required step fails, no alert is emitted and the reason remains visible.

### Manually release a dynamic symbol

1. The user chooses Release.
2. Edge refuses when a protection reason applies and shows that reason.
3. Edge disables evaluation, requests unsubscribe, and confirms removal.
4. The released symbol remains in the discovery/session record but leaves live
   rankings.

### Review candidates after hours

1. Schwab stops delivering live session updates.
2. The window labels the retained snapshot as closed/final rather than live.
3. The user sorts candidates and saves selected symbols to a named, described
   watchlist for later use.
4. Saving does not modify the completed session's membership or automatically
   select a next-start universe.

## Edge Cases And Failure Modes

- A screener list is acknowledged but sends no update: show subscribed/no-data,
  not disconnected.
- One service stalls while the WebSocket remains active: degrade that service
  independently and suspend automatic turnover if required evidence is stale.
- Schwab partially accepts a subscription chunk: reflect exact acknowledged
  membership and reject or retry only the discarded subset within rate limits.
- Schwab reports a lower capacity than expected: preserve protected leases,
  stop admissions, and expose the deficit.
- A candidate appears in several lists: merge provenance and recurrence rather
  than create duplicate rows or leases.
- The candidate disappears from one update: lower recurrence/retention evidence;
  do not immediately release it.
- History is absent, stale, or rate-limited: remain warming or fail with cooldown;
  never fabricate readiness.
- Bars arrive during history loading: buffer, merge, deduplicate, and reject old
  epochs.
- A reconnect occurs during promotion or release: invalidate the operation,
  restore desired membership under a new epoch, and revalidate readiness.
- A protection reason appears during eviction: cancel release before sending
  `UNSUBS`, or restore membership if the provider operation already completed.
- External quote-watch membership changes: update `external_watch` protection
  without claiming it is an open position.
- The selected watchlist is edited externally: it does not rewrite the current
  dynamic overlay; existing startup behavior remains authoritative until restart.
- Market close or half-day close occurs: stop automatic decisions, finalize the
  session snapshot, and retain review data.
- The process crashes during persistence: recover all complete records and
  ignore/quarantine a truncated final record.

## Acceptance Criteria

- [ ] Edge displays live normalized `SCREENER_EQUITY` rows in a dedicated Schwab
      Market Screener window using its existing Schwab WebSocket.
- [ ] A fixture proves that a screener symbol outside the startup universe appears
      in the window without becoming an alert or scanner state.
- [ ] Existing 5-Min Movers remains limited to ready live-universe symbols.
- [ ] Requested and acknowledged Screener, Chart, and Level One memberships are
      independently observable.
- [ ] Observe mode never changes Chart or Level One membership.
- [ ] Manual mode can admit an eligible outside symbol without restarting or
      interrupting existing stream services.
- [ ] Manual release removes an unprotected dynamic symbol without interrupting
      other memberships.
- [ ] Provider rejection, partial acceptance, and code-19 limit fixtures fail
      closed and preserve accurate acknowledged state.
- [ ] Live bars received during promotion warmup are buffered and merged without
      gaps or duplicate timestamps.
- [ ] A promoted symbol cannot emit an alert until its readiness gate passes.
- [ ] A failed warmup emits no alert, exposes the reason, and releases or holds
      capacity according to explicit operator state.
- [ ] Reconnect tests restore desired membership under a new epoch and reject late
      events from the previous epoch.
- [ ] Support, manual, external-watch, warming, residence, and operator-hold pins
      prevent automatic release.
- [ ] A single LOD or negative alert cannot by itself release a symbol.
- [ ] Shadow mode records decisions without sending membership changes.
- [ ] Automatic mode is disabled on upgrade and cannot be enabled before the
      required shadow observation period is satisfied or explicitly reset by the
      operator after review.
- [ ] Automatic reconciliation respects headroom, rate limits, residence,
      cooldown, hysteresis, protection, and service health.
- [ ] Final session candidates remain reviewable after hours and can be saved to
      existing named/described watchlists.
- [ ] Saving a candidate watchlist does not modify the current live overlay or
      select the startup universe.
- [ ] Existing watchlists, Yahoo screener, startup universe, alert feed, charts,
      and replay tests remain compatible.
- [ ] Dashboard tests, protocol fixtures, lifecycle tests, reconnect tests,
      backend suites, frontend lint/build, and rendered workflow verification pass.
- [ ] User documentation explains the three Schwab services, the four modes,
      capacity, readiness, protection, and after-hours workflow.

## Assumptions

- Schwab continues to support `ADD` and `UNSUBS` for the relevant services on
  one connection.
- The currently observed Chart Equity capacity is 300, but Schwab may report a
  different account-specific or future limit.
- Level One capacity is separate from Chart Equity capacity and may also change.
- The existing shared Schwab rate limiter can serialize promotion-time history
  acquisition with other REST activity.
- The external quote-watch list is the only current machine-readable protection
  handoff; it is not proof of an open position.
- The starter screener keys are valid for the account; rejected keys remain
  individually visible and configurable.
- Twenty retained trading sessions, five slots of Chart headroom, a 30-minute
  residence, a 15-minute cooldown, and two changes per five minutes are safe
  initial defaults subject to shadow-mode evidence.

## Risks

- Schwab streaming documentation and actual field/cap behavior can differ.
- Candidate history acquisition may be too slow for fast-moving names when the
  required cache baseline is absent.
- Dynamic state creation can expose assumptions that scanner structures are
  immutable after warmup.
- Aggressive ranking weights can cause churn or replace useful recovery names.
- Screener values can be stale or semantically different from Edge's calculated
  indicators.
- A provider reconnect can make requested membership diverge from reality.
- The dashboard may encourage users to mistake discovery rank for a trade signal.
- Retained discovery data can grow without strict bounds.

## Success Measures

- Zero additional Schwab WebSocket connections.
- Zero alerts from not-ready or ambiguously subscribed symbols.
- Zero protected-symbol automatic releases in fixture and shadow evidence.
- Every admission and release has a human-readable reason and structured audit
  evidence.
- At least five full sessions of Shadow mode complete without uncontrolled churn
  before Automatic mode is considered.
- Manual promotion with complete cached baseline reaches ready state within 30
  seconds in normal operation.
- Operators can identify and save outside-universe leaders after hours without
  confusing them with Edge alerts.

## Implementation Phases

### Phase 1: Specification And Architecture

- [x] Convert the approved plan into this v2 product specification.
- [ ] Approve the assumptions and initial defaults.
- [ ] Add the new domain terms to `CONTEXT.md`.
- [ ] Record the single-stream subscription and symbol-lifecycle ADR.

### Phase 2: Visible Discovery

- [ ] Implement the Schwab Screener Equity adapter and fixture normalization.
- [ ] Add candidate registry and bounded session persistence.
- [ ] Add the Schwab Market Screener window in Observe mode.
- [ ] Add after-hours review and save-to-watchlist workflow.

### Phase 3: Safe Manual Turnover

- [ ] Implement acknowledged Chart/Level One reconciliation and reconnect restore.
- [ ] Implement dynamic state warmup, buffering, merge, and readiness gating.
- [ ] Add manual Admit, Release, Pin, and Hold controls.
- [ ] Validate capacity, failure recovery, and alert isolation.

### Phase 4: Measured Automation

- [ ] Implement the long-only allocator behind the subscription-controller seam.
- [ ] Run Shadow mode for at least five complete sessions.
- [ ] Review churn, missed candidates, readiness time, and protection evidence.
- [ ] Tune and approve versioned thresholds from the recorded evidence.

### Phase 5: Guarded Release

- [ ] Enable explicit Automatic mode with safe disabled-by-default migration.
- [ ] Add emergency disable, health degradation, and operator recovery paths.
- [ ] Complete integration, frontend, reconnect, rendered, and regression tests.
- [ ] Update user, Config, operations, and troubleshooting documentation.

## Related Work

- [Dynamic Schwab discovery plan](../plans/dynamic-schwab-discovery-plan.md)
- [Screener and Watchlist Universe v1.0 PRD](screener-universe-v1.0-prd.md)
- #24 expands saved-screener metrics but does not block Schwab stream discovery.
- #74 exposes stored bars to downstream consumers and does not own promotion
  warmup.
- #20 is not required for this feature.
- #21 is postponed and is not implemented here.

## Clarification Log

- Round 1: The source plan established one Schwab connection, separate discovery
  and live-universe roles, runtime subscription changes, a new screener window,
  after-hours retention, long-only turnover, readiness gating, and staged
  automation.
- Round 2: The user removed #20 from scope and postponed #21.
- Conversion assumptions: Observe is the upgrade default; dynamic membership is
  session-only; existing watchlists remain unchanged; conservative capacity,
  residence, cooldown, retention, and rate defaults are recorded under
  Assumptions for explicit approval.

## Metadata

- Created: 2026-09-25
- Source: `docs/plans/dynamic-schwab-discovery-plan.md`
- Clarification rounds: 2 source-plan decisions plus explicit conversion assumptions
- Final clarity score: 94/100
