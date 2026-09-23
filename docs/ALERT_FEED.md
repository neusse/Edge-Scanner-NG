# Alert feed integration contract (v1)

Edge Scanner publishes observations, **not orders or permission to trade**. A trading application owns account state, duplicate suppression, market-hours checks, sizing, buying power, risk limits, order validation, and its own authorization policy. In particular, an `Exit Trade Watch` alert is a long-position exit observation, not an instruction to open a short.

For market-timed bid/ask, last trade, spread, quote quality, and held-symbol watch registration, use the separate [live quote contract](QUOTE_FEED.md). Alert `market_timestamp` remains the bar/setup time, not a proof of current tradable liquidity. When a spread condition is configured, `alert.quote` holds the shared quote observation used by that gate.

The machine-readable contract is [alert-feed-v1.schema.json](schemas/alert-feed-v1.schema.json); representative [system, custom, exit-watch and replay fixtures](../tests/fixtures/alert_feed_v1.json) are tested against it. The [reference consumer](../scripts/consume_alerts.py) logs alerts, reconnects, recovers and never submits orders.

## Endpoints and connection lifecycle

| Endpoint | Purpose |
|---|---|
| `ws://localhost:7777/ws/alerts` | One WebSocket for initial backlog, live alerts, status and heartbeat frames. |
| `GET http://localhost:7777/api/alerts` | Recent in-memory alerts, newest first; dashboard-compatible, not a recovery cursor. |
| `GET http://localhost:7777/api/alerts/recover?after=EVENT_ID&limit=500` | Retained archive, oldest first; omit `after` to start at the oldest retained alert. |
| `GET http://localhost:7777/api/feed/status` | Current scanner/feed status. |

The WebSocket's first frame is `type: "replay"`: up to 500 matching recent alerts in **newest-first** order. This is a connection backlog even when the scanner's `mode` is `live`; it is not a simulated market replay. `truncated: true` means older matching alerts did not fit in that 500-alert frame. It says nothing about alerts beyond the in-memory buffer (default 5,000) or archive retention. Use the recovery endpoint for a complete retained-range read. Subsequent `type: "alert"` frames are emitted in scanner publication order. `type: "status"` appears on a lifecycle change; `type: "heartbeat"` appears every 5 seconds while the API runs. Allow 15 seconds without a frame before reconnecting. A slow client with a 1,000-frame outbox or a send stalled over 5 seconds is disconnected (WebSocket code 1013); recover after reconnect.

Status `state` is `warming_up`, `connecting`, `live`, `stale`, or `stopping`. `live` begins on the first market bar. `stale` means no bar was received for 90 seconds, or connecting lasted that long; a heartbeat proves the API is responsive, **not** that Schwab is healthy. `last_market_timestamp` is the latest bar's market time, or null. `market_age_seconds` is time since local receipt, or null. On a full scanner shutdown the socket disappears; do not assume a final status frame always reaches the client.

## Alert identity and fields

Every new alert has `schema_version: 1`, `event_id`, `session_id`, process-local positive `seq`, `mode` (`live` or `replay`), `market_timestamp`, `emitted_at`, `source`, and `archive_write_ok`. `event_id` is `session_id:seq`; the session ID is a new random value for each scanner process. Persist the whole event ID for idempotency, **never `seq` alone**. The original producer fields remain inside `alert` for existing dashboard consumers. The `type: "alert"` frame repeats key envelope fields at top level. The initial backlog and heartbeat/status frames have their own frame envelopes. Unknown/additive fields are allowed.

For a backlog, heartbeat or status frame, top-level `seq` and `event_id` are the latest-published alert watermark (0 and null before the first alert), **not identities of the control frame**. Its `market_timestamp` is the latest received market bar time or null.

| Field | Meaning |
|---|---|
| `mode` | `live` is current scanning; `replay` is a simulated/historical run. An old archive record without provenance is marked `unknown` inside its alert and must not be treated as live. |
| `market_timestamp` | UTC, timezone-aware ISO 8601 market/bar time; null if the producer supplied no timezone-aware event time. It is **not** a quote or order timestamp. |
| `emitted_at` | UTC, timezone-aware ISO 8601 time Edge published the alert. Old imported records have null because their original emission time is unknown. |
| `archive_write_ok` | The JSONL append returned successfully. `false` means this live event may not be recoverable. This is not an fsync or exactly-once guarantee. |
| `source` | `system` (plugin setup), `custom` (configured setup), or `unknown` for an old record whose source is missing. |
| `symbol`, `setup`, `setup_label` | Instrument ticker, stable setup ID, and changeable display name. A setup rename does not change its ID. |
| `trigger`, `entry_trigger`, `trigger_evidence` | Trigger identity; for composed setups, evidence lists the satisfying triggers, including earlier bars in the configured window. |
| `direction` | Price/setup direction (`long`, `short`, `neutral`), **not an order side**. Read the setup semantics before acting. |
| `price`, `suggested_stop` | USD per share, floating-point observations/suggestions; a null or absent stop is unavailable, not zero. |
| `score` | Unitless setup score (typically 0–100); missing/null is unavailable. |
| `stop_pct`, `pct_change` | Fractions (`0.02` means 2%), not percent-point numbers. |
| `rvol` | Unitless relative-volume multiple. |
| `session` | Producer's market session label, generally `pre`, `rth`, or `post`. |
| `context`, `warnings`, `gates` | Producer evidence and diagnostics; nested keys are additive and may be absent. |

Unless a field is listed as required in the schema, it may be absent. Nullable fields use JSON `null` for unknown; consumers must not substitute zero, `false`, or an empty string. Precision is the source/provider's available precision; do not infer executable quote freshness from an alert price or minute-bar timestamp.

## Filters

The WebSocket, recent REST and recovery REST endpoints accept the same query parameters. Comma-separated values are ORed within a parameter; different parameters are ANDed. An omitted/empty parameter means no restriction. Setups and triggers match **exact, case-sensitive IDs**; symbols are case-insensitive and normalized to uppercase. Source and direction are case-insensitive. `triggers` matches either `trigger` or `entry_trigger`. `min_score` is inclusive; an alert without a numeric score does not pass it.

| Parameter | Values |
|---|---|
| `sources` | `system`, `custom`, comma-separated; `all` or `*` means unrestricted. |
| `setups` | Exact setup IDs, comma-separated. |
| `triggers` | Exact trigger IDs, comma-separated. |
| `symbols` | Tickers, comma-separated. |
| `direction` | `long`, `short`, `neutral`. |
| `min_score` | Finite number. |
| `custom` | `1`, `true`, `yes`, `only` for custom; `0`, `false`, `no` for non-custom. |
| `limit` | REST only: recent endpoint 1–5,000; recovery endpoint 1–500 (values are clamped). |

For example: `ws://localhost:7777/ws/alerts?symbols=NVDA,AAPL&direction=long`. An invalid source, direction, `min_score`, or `custom` value returns HTTP 400 on REST or closes the WebSocket with code 1008. Unknown query keys are ignored. Do not use an invalid filter as a safety gate.

## Delivery, recovery and retention

Delivery is **best-effort**, not exactly-once or at-least-once. Publication and the per-process queue are ordered by `seq`; a healthy client's initial replay and subsequent live frames do not overlap within that connection. A disconnect, crash, full queue, archive write failure, or retention expiry can still cause a gap. Consumers must deduplicate `event_id` and recover after every reconnect:

1. Persist the last processed `event_id` locally.
2. `GET /api/alerts/recover?after=...` and page while `has_more` is true, processing oldest first. The response's `next_cursor` is the last returned matching event ID.
3. Connect WebSocket, read its first backlog frame, and recover once more if it is truncated or you already have a cursor. This closes the read/connect race; deduplicate overlap by event ID.
4. Process live frames, and on disconnect or 15 seconds without a heartbeat repeat from step 2. For an **unfiltered** feed, a same-session `seq` jump is another reason to recover; filtered feeds naturally skip sequence values.

Recovery returns HTTP 410 `cursor_not_found` if `after` is not in the retained archive. That is an explicit unrecoverable gap: halt or require operator review; do not silently resume from the newest alert. The archive is `data/alerts/all/YYYY-MM-DD.jsonl`, retained for `--keep-days` calendar days (default 5) and written on the scanner machine's local date. A restart gives a new `session_id` and resets `seq`, but retained event IDs do not change. An initial `after`-less recovery gives the oldest retained page, not all historical alerts ever produced. Old pre-v1 archive records get stable `legacy:` IDs and `mode: "unknown"`; a live-execution consumer must reject them. An alert marked `archive_write_ok: false` cannot be guaranteed recoverable.

Run the read-only example with:

```powershell
.\.venv\Scripts\python.exe scripts\consume_alerts.py --url ws://localhost:7777/ws/alerts --cursor-file data\consumer-alert-cursor.txt
```

Its cursor file is consumer-owned state; do not share one cursor among independent consumers. It logs replay/unknown-mode alerts as non-live, exits on an unrecoverable archive gap, and has no order API or credentials.

## Compatibility and security

Version 1 permits additive optional fields and new `context` keys. Existing required field meaning, units, ordering, or enum semantics will not change within v1. A breaking change needs a new schema/endpoint version with a documented overlap period; a consumer should reject unknown schema versions until updated. Existing dashboard clients still receive the `replay` and `alert` frame shapes they use; the new fields and heartbeat/status types are additive.

The default API bind is loopback only (`127.0.0.1` plus loopback IPv6 where available). There is **no API authentication or TLS**. Do not expose the feed on a LAN or public interface as a trading integration without an authenticated, encrypted transport design. The WebSocket checks local browser Origin, but Origin is not client authentication. Never put broker tokens in feed messages, logs, fixtures or status endpoints.

If a consumer sees no alerts, first check `/api/feed/status` and the 5-second WebSocket heartbeat, then the scanner terminal's market-bar heartbeat, the filter values, and whether the current session has qualifying setups. `stale`, repeated 1013 disconnects, a 410 recovery response, or `archive_write_ok: false` require investigation before relying on the feed.
