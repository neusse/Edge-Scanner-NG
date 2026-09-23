# Live quote and spread contract

Edge retains current Schwab Level One equity observations alongside its bar scanner. It uses the **existing Schwab stream**, not another token/session or dashboard WebSocket. Quotes are observations, not executable prices or permission to trade. A consumer must apply its own freshness, market-session, broker, and risk rules. See the [alert-feed contract](ALERT_FEED.md) for alert identity and recovery; alert bar time is not quote time.

## Read and watch

| Endpoint | Meaning |
|---|---|
| `GET /api/v2/quotes/AMD` | One current observation. `404` means no covered/observed row exists. |
| `GET /api/v2/quotes/AMD/history?limit=600` | Up to 1,800 recent in-memory samples, oldest first. |
| `GET /api/v2/quotes/updates?since=0&limit=200` | Incremental updates, oldest first, up to 1,000 per call. |
| `PUT /api/v2/quotes/watch` with `{"symbols":["AMD","HELD"]}` | Replace the complete external held-symbol watch set (maximum 64), adding/removing subscriptions on the same Schwab stream. Do this whenever holdings change; include every held symbol to retain. |
| `GET /api/v2/snapshot?symbols=AMD` | Existing scanner row plus optional `quote` object for in-universe symbols. A held symbol outside the scanner universe is read through `/api/v2/quotes/{symbol}`. |

The read APIs and dashboard do not subscribe on demand. A new symbol needs to be in the active scanner universe or explicitly watched. A quote watch is not a scanner-universe change and does not create alerts or bars. The default server binds to loopback; these endpoints have no application authentication, so do not expose them to an untrusted network.

`stream_id` changes each scanner process; `seq` is process-local and monotonic. Store both as a consumer cursor and request pages until caught up. If `stream_id` changes, discard the old cursor and fetch current rows. `oldest_seq` and `gap: true` mean the bounded update ring has overwritten unconsumed events; discard assumptions about continuity and read current rows again. There is no cross-process quote cursor or persistent quote archive. Quotes may be stale across reconnect, market close, or a scanner restart; never carry a previous-process observation into a new session as fresh.

## Fields and quality

Prices (`bid`, `ask`, `last`, `midpoint`, `spread`) are dollars per share. `spread = ask - bid`; `spread_bps = 10000 * spread / midpoint`. `null` means unavailable, never zero. The side/trade `*_market_ms` fields are Schwab market epoch milliseconds and `receipt_ms` is this process's local reception epoch milliseconds. `*_age_ms` is measured from market time when read. A caller with a two-second entry rule should require its own chosen fields to have non-null market timestamps, `delayed === false`, `quality === "valid"`, and each required age at most 2,000 ms. The scanner's display `stale` threshold is 10,000 ms and is **not** a trading threshold. Quote-only changes update this state even when bar volume does not move.

`bid_size`, `ask_size`, and `last_size` are the raw integers Schwab's Level One or quote endpoint supplies. Schwab platform display rules for sizes can vary; this API does not multiply by round lots or present the values as book depth or executed directional volume. `source` is `schwab_levelone` or `schwab_rest`, `tier` is `stream` or `poll`, `coverage` is `subscribing`, `live`, `cap_exceeded`, or `not_watched`, `session` is derived from the newest market timestamp in US/Eastern time, and `delayed` is the provider flag when supplied (`null` when unknown). REST-poll quotes can be lower-frequency and should not pass a strict entry rule unless their market timestamps and latency independently qualify.

`quality` is `valid`, `missing`, `invalid`, `locked`, `crossed`, `delayed`, `unverified_time`, `stale`, or `unavailable`. Missing or invalid prices never generate a zero spread. Locked means bid equals ask; crossed means ask is below bid. Sparse updates retain prior fields *and their original timestamps*, so a fresh ask cannot make an old bid fresh. Older market timestamps for an individual side/trade are rejected. The optional `spread_bps` setup condition fails closed unless the quote is valid, explicitly non-delayed, and **both** sides meet its configured market-time age (default 2,000 ms). Its alert carries the quote observation used for that decision in `alert.quote`.

## History and dashboard

The Bid / Ask window follows its link group or a typed symbol. It plots bid and ask in the top pane and spread basis points below. It draws whitespace for invalid/stale observations and gaps longer than three seconds, not a continuous fresh line. History keeps the **latest observation per receipt second** for each covered symbol, at most 1,800 samples per symbol and 250,000 samples across all symbols; it is in memory and is lost on restart. The update ring holds 10,000 events for all symbols. Neither minute bars nor stored OHLCV history can reconstruct earlier bid/ask. The display polls these bounded REST reads every two seconds; this is **not** another Schwab market-data connection.

Schwab's account Level One budget is currently 3,000 symbols. Quote coverage includes the CHART_EQUITY bar symbols, leaving the remainder for quote-derived bars; any overflow moves to REST polling. The API exposes each symbol's source/tier and coverage so callers can reject unavailable or slow paths. The 300 CHART_EQUITY budget remains separate. If a held-symbol watch would exceed the Level One budget, the watch update fails rather than silently displacing an active symbol.
