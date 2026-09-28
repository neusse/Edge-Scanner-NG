# ADR-0001: One Schwab stream with observe-only discovery

## Status

Accepted — 2026-09-25

## Context

Schwab permits one streaming connection for the account. `SCREENER_EQUITY` can surface market-wide discovery candidates that are not in Edge's bounded live universe. Treating those rows as scanner state would silently expand live coverage and blur discovery with alert evaluation.

## Decision

- Edge subscribes to `SCREENER_EQUITY` on the existing Schwab WebSocket used by `CHART_EQUITY` and `LEVELONE_EQUITIES`.
- Provider rows are normalized into a separate observation book. They do not create `SymbolState` objects, setups, alerts, or chart/quote subscriptions.
- The Schwab Market Screener window is explicitly **Observe only**. It exposes provider provenance, receipt age, health, rank, values, and whether each row is already in the live universe.
- 5-Min Movers remains a ranking of current live-universe symbols. It is not renamed or repurposed as whole-market discovery.
- Admission and release policy is separate work. Observation has no endpoint or UI control that can change live membership.
- Screener-list configuration is session-wide and may change only `SCREENER_EQUITY` keys. Each key is sent as a separate request so its acknowledgement, rejection, and health remain independent.

## Consequences

Outside-universe candidates are visible without spending a live-universe slot. A rejected or malformed screener message cannot interrupt bar ingestion. Any later turnover automation must cross an explicit admission-policy boundary rather than mutating the scanner as a side effect of discovery.
Repeated appearances are merged in a session-scoped candidate registry; persistence across completed sessions remains separate work.
