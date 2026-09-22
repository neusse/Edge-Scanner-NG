# Screener And Watchlist Universe - Product Requirements Document

## Summary

- Problem: Traders need to discover a focused set of symbols without subscribing Schwab to an unknown or ever-growing union of lists.
- Target users: A local Edge Scanner NG user preparing a known universe before a trading session.
- Proposed solution: Add a Yahoo-backed Screener window that saves snapshots to described watchlists, and allow exactly one watchlist to be selected as the scanner universe for the next restart.
- Version: 1.0
- Status: Approved for implementation

## Goals

- Discover US equity candidates outside the currently streamed universe through yfinance screening.
- Save all or selected results as a named, described watchlist.
- Support create, merge, and replace saves, with create as the default.
- Select exactly one watchlist as the next-start scanner universe.
- Make Schwab capacity explicit and prevent an oversized selected universe from starting.
- Keep watchlists editable through the UI and out of band.

## Non-Goals

- Automatically merge every watchlist into the scanner universe.
- Change subscriptions silently during a live session.
- Treat Yahoo data as execution-quality market data.
- Submit orders or add broker controls.
- Continuously synchronize a watchlist with changing screener results.

## Requirements

### Functional Requirements

- Add a Screener dashboard window with Yahoo presets and a bounded custom filter builder.
- Show result count, sortable market-data columns, row selection, refresh, and linked-symbol behavior.
- Save selected rows, or all rows when none are selected, to a watchlist.
- Add watchlist descriptions and source/capture metadata.
- Allow screener saves to create, merge, or replace a watchlist.
- Allow one watchlist to be assigned as the next scanner universe.
- Display active stocks, required support symbols, and total Schwab chart-stream usage.
- Apply a selected universe only at scanner startup.
- Add text/CSV import and export for out-of-band watchlist maintenance.

### Data Requirements

- Watchlists remain in `data/watchlists.json` and preserve existing records.
- Universe selection is stored separately and references one watchlist id.
- Direct yfinance calls supply discovery candidates; Schwab remains authoritative for live data.
- The selected list preserves symbol order and removes duplicates.

### Integration Requirements

- Use the installed yfinance `screen()` and `EquityQuery` APIs from the backend.
- Reuse the existing dashboard window registry, virtual table, link groups, and watchlist store.
- Reserve stream capacity for SPY and sector ETFs used by relative-strength calculations.

### Constraints

- Performance: Cache equivalent Yahoo queries briefly and refresh only on user request or a conservative interval.
- Compatibility: Existing screens and watchlists must load without migration steps.
- Security: No credentials or account information are stored in screener/watchlist records.
- Operations: An invalid or oversized externally edited active watchlist must fail startup clearly; never truncate or broaden it silently.

## User Workflow

1. Add a Screener window.
2. Choose a Yahoo preset or custom filters and refresh.
3. Inspect candidates through linked chart, news, and stock-information windows.
4. Select rows or leave all rows selected implicitly.
5. Save to a new watchlist or explicitly merge/replace an existing watchlist.
6. Edit the watchlist name and description as needed.
7. Assign one watchlist as the next scanner universe.
8. Review the capacity calculation and restart the scanner to apply it.

## Edge Cases And Failure Modes

- Missing yfinance returns a readable unavailable error.
- Yahoo returning OTC, non-equity, or non-USD rows is filtered by default.
- Empty results remain save-disabled.
- Deleting the selected watchlist clears the pending selection.
- Saving an active watchlist over the safe limit is rejected.
- An out-of-band oversized selected list stops startup with the exact counts and repair path.
- Missing selected watchlist stops startup instead of falling back to another universe.

## Acceptance Criteria

- [x] Screener is available from Add Window and follows existing dashboard styling.
- [x] Preset and custom Yahoo queries return normalized candidate rows.
- [x] Selected/all results can create, merge, or replace watchlists.
- [x] Watchlist descriptions persist and are editable.
- [x] Watchlists import/export newline text and CSV symbol lists.
- [x] Exactly one watchlist can be assigned as the next-start universe.
- [x] UI and startup both enforce the real Schwab total, including support symbols.
- [x] The current live scanner is unchanged until restart.
- [x] Python tests, frontend lint, frontend build, and rendered workflow verification pass.
- [x] README and user guide describe the workflow and limits.

## Assumptions

- Yahoo is suitable for discovery but not for execution decisions.
- The current Schwab CHART_EQUITY cap is 300.
- The support-symbol set is computed rather than permanently hard-coded.
- Out-of-band edits are safest while the scanner/dashboard is stopped; import/export is the preferred path.

## Risks

- Yahoo fields and predefined screen names may change.
- A selected symbol may be unavailable from Schwab or lack sufficient history.
- Broad candidate lists can make first-time history synchronization slow.

## Implementation Phases

### Phase 1: Preparation

- [x] Confirm yfinance MCP and direct-library screening capabilities.
- [x] Define watchlist versus screener versus active-universe boundaries.

### Phase 2: Core Implementation

- [x] Extend watchlist persistence and add universe-selection persistence.
- [x] Add Yahoo screener service and API routes.
- [x] Add Screener window, watchlist save flow, import/export, and universe assignment.
- [x] Apply selected watchlist during startup with hard validation.

### Phase 3: Validation

- [x] Add backend and frontend tests where supported.
- [x] Run Python tests, lint, and production build.
- [x] Verify the rendered workflow in the browser.

### Phase 4: Release Or Handoff

- [ ] Update user documentation.
- [ ] Summarize behavior, limits, and any deferred work.

## Clarification Log

- Round 1: Screener is a dashboard window; reusable filter definitions remain configuration.
- Round 2: Screener results are snapshots saved to named, described watchlists.
- Round 3: Exactly one watchlist, not a union, is selected as the next-start universe.
- Round 4: Existing-list saves offer merge and replace; create new is the default.
- Round 5: Direct yfinance calls are preferred at runtime; the installed MCP confirmed the capability.

## Metadata

- Created: 2026-09-21
- Clarification rounds: 5
- Final clarity score: 100/100
