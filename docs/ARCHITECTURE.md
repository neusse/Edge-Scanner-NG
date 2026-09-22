# Edge Scanner NG architecture

This documentation set explains how Edge Scanner acquires market data, updates
per-symbol state, evaluates setups, publishes accepted alerts, and serves the
local dashboard and other alert consumers.

The diagrams are interactive standalone HTML. They support light and dark
themes, search, pan and zoom, guided views, presentation mode, and image export.

## Interactive diagrams

| View | Question it answers | GitHub Pages |
|---|---|---|
| System architecture | What runs locally, what is external, and where are the execution boundaries? | [Open system architecture](https://neusse.github.io/Edge-Scanner-NG/architecture/edge-scanner-system.html) |
| Alert data flow | How do provider data, caches, configuration, setup evaluation, archives, and consumers connect? | [Open alert data flow](https://neusse.github.io/Edge-Scanner-NG/architecture/alert-pipeline.html) |
| Live-alert sequence | What happens from one completed minute bar through WebSocket delivery? | [Open live-alert sequence](https://neusse.github.io/Edge-Scanner-NG/architecture/live-alert.html) |

The [Pages architecture index](https://neusse.github.io/Edge-Scanner-NG/architecture/)
collects the same diagrams as a browsable report.

## Architectural summary

Edge Scanner is a local-first, single-process application. `scripts/run_live.py`
loads the universe and historical caches, creates one selected `DataFeed`, seeds
scanner state, starts the FastAPI server, and then enters the provider's blocking
minute-bar subscription.

Provider-specific code normalizes Alpaca or Schwab data behind the `DataFeed`
interface. Each completed minute bar advances `LiveScanner` and its per-symbol
state. Indicators and session levels update before enabled setup evaluators run.
An `AlertSink` applies repeat/cooldown acceptance; the unified `FeedHub` then
archives accepted alerts and fans them out to matching WebSocket subscribers.

FastAPI serves both the React dashboard APIs and the unified alert feed on
localhost port 7777. The dashboard and external local consumers observe the same
accepted-alert stream. Edge Scanner does not place orders: any trading program
must independently own idempotency, account state, risk limits, sizing, order
validation, and execution authorization.

## Source evidence

The system architecture is pinned to repository revision
`a9a9d7854c9a11cd9cceea5dfae267286d18200c`. Its evidence links cover the
startup orchestrator, provider interface and adapters, live scanner, state and
indicators, setup catalogs/evaluator, alert hub, API, local stores, and dashboard
clients. The data-flow and sequence diagrams are authored interpretations of
those same code paths.

Important implementation entry points:

- `scripts/run_live.py` — startup, history seeding, API launch, and live connection
- `scanner/data/interface.py` — provider contract
- `scanner/data/alpaca.py` and `scanner/data/schwab.py` — provider adapters
- `scanner/live_scanner.py` and `scanner/state.py` — minute-bar processing and symbol state
- `scanner/custom_setups.py`, `scanner/trigger_catalog.py`, and `scanner/conditions.py` — setup evaluation vocabulary
- `scanner/feed_hub.py` — accepted-alert archive, replay, filters, and fan-out
- `scanner/api.py` and `scanner/api_v2.py` — WebSocket, REST, and dashboard serving
- `dashboard-v2/src/` — local React dashboard

## Current integration boundary

The existing alert WebSocket begins with a bounded replay frame and then sends
live alert frames. Slow subscribers are disconnected instead of silently losing
individual queued messages. The current `seq` value is process-local and resets
when the scanner restarts; it is not yet a durable cross-restart event identity.
[Issue #17](https://github.com/neusse/Edge-Scanner-NG/issues/17) tracks the
versioned schema, durable identity, heartbeat, recovery, and reference consumer
needed before treating the feed as a formal trading integration API.

## Validation

All three sources pass Archify's showcase profile with all nine artifact checks,
zero composition errors, and zero warnings. Browser checks pass containment,
readability, viewer controls, and light/dark capture at 1440×900, 1600×1000,
1920×1080, and 2048×1320. The generated HTML is self-contained and requires no
Archify or Node.js runtime when served.

Typed sources are retained under `docs/architecture/sources/` so the diagrams
can be regenerated and reviewed when the architecture changes.
