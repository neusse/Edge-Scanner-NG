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
