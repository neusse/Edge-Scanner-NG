# Edge Scanner language

Edge observes market data and publishes watch alerts. An alert describes an observed setup; it is not a trade decision or an order.

## Language

**Indicator**:
A calculated market measure, such as VWAP, an EMA, ATR, or relative volume. An indicator value alone is not an event.

**Trigger**:
An observable event, such as price crossing a level or two moving averages crossing. A trigger may be combined with other triggers inside a setup.
_Avoid_: Alert, signal (when referring to a reusable event rule)

**Check**:
A requirement about the current market state that must pass before a setup publishes an alert. Checks do not themselves announce an event.
_Avoid_: Parameter, filter (when referring to a changing market-state requirement)

**Universe filter**:
A named screen that limits which symbols a setup considers using characteristics fixed for the session.
_Avoid_: Check (for session-fixed stock selection)

**Setup**:
A named detection rule that combines triggers, checks, an eligible universe, and repeat behavior. A setup describes what Edge watches for, not what Trader should execute.
_Avoid_: Strategy, trade signal

**Alert**:
A published observation that a setup qualified for a symbol at a particular market time, with its supporting evidence.
_Avoid_: Trigger, order, trade recommendation

**Tight range**:
A bounded recent price range. It does not imply that volatility or volume was progressively contracting.
_Avoid_: Compression

**Compression**:
A pattern in which price movement contracts over time, usually accompanied by declining participation before any expansion. A single narrow range is insufficient to establish it.

**Discovery list**:
A provider-ranked list of stocks that may deserve attention. Discovery is observation only and does not grant live market-data coverage.
_Avoid_: Universe, watchlist (unless the rows have actually been saved)

**Candidate**:
A symbol observed through discovery but not necessarily present in the live universe. A candidate cannot produce setups or alerts until separately admitted to the live universe.
_Avoid_: Scanner symbol, alert

**Live universe**:
The bounded set of symbols for which Edge maintains scanner state and evaluates setups. Provider discovery rows outside this set remain candidates.
_Avoid_: Screener results, entire market

**Manual admission**:
An operator request to add one current discovery candidate to Chart Equity and Level One on Edge's existing Schwab connection, then warm its chart state. Admission is not a setup, alert, or trade decision.

**Readiness**:
The explicit state reached after provider acknowledgements, cache-first history inspection, live-bar buffering, and a gap-checked merge. Chart readiness and setup readiness remain separate: each enabled setup must prove its daily, intraday, session, sector, quote, and indicator inputs before it can evaluate the promoted symbol.

**Setup availability**:
The per-symbol, per-setup result of the readiness gate: `ready`, `unavailable` because required input is missing, or `filtered` because the symbol does not belong to that setup's universe profile. Only `ready` setup IDs may publish observations.
