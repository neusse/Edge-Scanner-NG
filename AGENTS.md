## Agent skills

### Issue tracker

Issues and specifications are tracked in GitHub Issues for `neusse/Edge-Scanner-NG`. See `docs/agents/issue-tracker.md`.

### Triage labels

Use the five default triage labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repository. See `docs/agents/domain.md`.

### Candle timing and trading boundary

- Treat the completed one-minute bar as the scanner's input clock. A multi-minute candle is eligible for pattern evaluation only after every constituent minute has arrived and its interval has closed; missing minutes fail closed, including after a restart.
- Use completed five-minute candles to confirm structural day-trading patterns (for example Trend and Failed Swing Break). Use completed one-minute candles for explicitly fast watch/exit events (for example Exit Trade Watch). Do not treat an in-progress wick as a closed-candle confirmation.
- Keep the evaluated one-minute `source_bar` distinct from a trigger's configured timeframe and evidence. Test timing, gap/restart behavior, and chart alignment when changing either path. See `docs/SETUP_REFERENCE.md` and `docs/ALERT_FEED.md`.
- Edge publishes observations, not entry or exit orders. Trader owns position-aware decisions and protective stops; an Edge alert is not a substitute for a live risk control.
