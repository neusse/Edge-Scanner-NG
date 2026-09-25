# Alert replay

Replay runs Edge's existing 1-minute bar evaluator over a frozen past session. It is
for checking alert behavior, not for trading or simulating fills. Replay opens **no
Schwab stream** and never sends orders. The live scanner and replay are mutually
exclusive on this installation. Stop the live scanner first; replay refuses to
start if it finds a sibling or the live dashboard port in use.

## Start

From a visible PowerShell terminal in this project:

```powershell
.\start_replay.bat --date 2026-09-22 --symbols AAPL,MSFT --speed 60
```

Omit `--symbols` to use `data/universe.csv`, or pass `--universe` with a CSV that
has a `symbol` column. The first run fetches prior daily and 5-minute context
and that day's 1-minute bars through Schwab REST, then freezes both bars and the
current setup/profile/settings files in `data/replay/inputs/<date>-<id>/`.
For a large universe this can take a while and is subject to Schwab's historical
1-minute availability. A failed capture is rejected rather than silently
replaying a partial universe.

If your registered Schwab callback URL ends in `/`, keep that exact URL in your
environment. This checkout's installed schwabdev rejects a trailing slash when
constructing its client, even though a new OAuth login must use the registered
URL. Replay therefore needs an existing, current token imported from
`SCHWAB_TOKEN_PATH`; it does not change the environment URL or perform a new
OAuth login. If that login has expired, renew it with the client that owns the
shared token before retrying replay.

Open `http://localhost:7778/v2/`. The top bar shows **REPLAY** and controls for
pause/resume, one timestamp-step, speed and reset. `--paused` starts stopped.
`--start 10:00 --end 12:00` limits the visible alert window, but earlier bars
still run so VWAP, opening range, EMA and cooldown state are not fabricated at
10:00. Ctrl+C stops replay. `--exit-on-complete` is useful for unattended test
runs; otherwise the completed page remains available until you stop it.

Rerun the exact captured market data and configuration without another Schwab
request:

```powershell
.\start_replay.bat --input "data\replay\inputs\2026-09-22-<id>" --speed 0 --exit-on-complete
```

The launcher verifies every captured file's checksum. Each playback or reset
has its own `data/replay/runs/<date>-<id>/pass-N/alerts/all/` archive and
`summary.json` with data, configuration and scanner-code fingerprints; it never
writes to the live `data/alerts/all/` archive. Replay
alerts carry `mode: "replay"` on the same versioned alert contract. The replay
feed is on port 7778, not the live port 7777. A downstream trading engine
should require `mode: "live"` before considering an alert.

## Boundaries

- The bundled core evaluates custom setups. An optional private system-setup
  plugin absent from this checkout cannot be replayed or claimed as tested.
- Historic/as-of fundamentals and news are not part of the frozen bundle.
  Fundamental-dependent universe profiles are rejected; the replay API does
  not serve today's fundamentals or news as if they were historic.
- This is bar-close replay. Sub-minute trade-cross triggers need recorded trade
  events, which this input format does not contain. A snapshot enabling one is
  rejected instead of silently reporting a false negative.
- The dashboard's configuration is read-only during replay. To compare edits,
  stop replay, edit live configuration, capture a new frozen input, and compare
  the separate run summaries. An interactive edit-and-rerun workspace is
  tracked separately in issue #28.
- Alerts retain the detector's logic; replay is not a profitability backtest.
  It does not model fills, slippage, positions or orders.

Replay implementation and acceptance work are tracked in issue #16.
