# Edge Scanner NG handoff

## Metadata
- Project: Edge Scanner NG
- Repository Root: `C:\Users\georg\Documents\Codex\Edge-Scanner-NG`
- Branch: `main`
- Last Code Commit: `e2362e9 Merge local Schwab discovery and scanner work` (this handoff is committed afterward)
- Last Updated Local: `2026-09-28 11:12 PDT`
- Last Updated UTC: `2026-09-28T18:12:14Z`
- Stale After Hours: `24`
- Staleness: `FRESH` as of the timestamp above; recheck GitHub and the running scanner on pickup.
- Owner: George Neusse

## Current Objective
Validate the now-published Observe/Manual Schwab discovery workflow in a live session, then complete the Shadow → guarded Automatic → verification sequence without turning discovery into trading decisions.

## Current State
- GitHub and clean-checkout `main` both point to `e2362e96b4c41a2d1272b081fd68a1fd97679562`; no open pull requests at handoff.
- The formerly unpublished local work is merged: Schwab screener UI and session history, candidate/readiness and manual admission/release, startup stream buffering, candle-behavior design, Alpaca probe, and related docs/tests. The prior safety/trigger work at `7d688e6` is retained.
- The separate running checkout at `C:\Users\georg\Codex_Projects\edge-scanner` remains on `codex/local-work-snapshot-20260928` (`e0010f9`) so its live scanner's files were not swapped mid-session. Its local `main` ref and `origin/main` are aligned to `e2362e9`. Its untracked `yfinance_earnings.py` is a separate scratch file and was deliberately not published.

## Operational Rules In Force
- Observe-only Schwab discovery is the default; a discovery candidate is not a live-universe member, setup, alert, or trade.
- One Schwab WebSocket. Manual admission needs acknowledged Chart Equity and Level One membership plus data readiness before setup evaluation. Automatic turnover remains disabled and unimplemented.
- Keep `HANDOFF.md` current-state-only; archive any prior handoff under `docs/archive/handoff-history/` if needed.

## Current Status
Published code and deterministic checks are green. Live Schwab verification remains pending because the active scanner still runs from the preserved older checkout; Shadow and Automatic are future phases, not part of this merge.

## Completed Since Last Handoff
- Merged the previously tested safety/trigger fixes and the local Schwab work into `main`, pushed to `neusse/Edge-Scanner-NG`, and reconciled the one trigger-count conflict and local-host API test.
- Integrated validation in the clean checkout: `990 passed, 6 skipped` (Python); dashboard `npm run build` passed; `npm run lint` passed with 5 warnings and no errors.

## In Progress
- [#76](https://github.com/neusse/Edge-Scanner-NG/issues/76): Dynamic Schwab discovery. Observe/Manual code is published; obtain live-session evidence before treating it as field-verified.
- [#84](https://github.com/neusse/Edge-Scanner-NG/issues/84): Shadow ranking and decision recording. Next implementation stage; no subscription changes in Shadow.
- [#85](https://github.com/neusse/Edge-Scanner-NG/issues/85): Guarded Automatic turnover, only after #84 and its observation requirement. Disabled by default.
- [#86](https://github.com/neusse/Edge-Scanner-NG/issues/86): End-to-end and documentation verification after #85.
- [#75](https://github.com/neusse/Edge-Scanner-NG/issues/75): Replay-tune the one-minute Momentum Run draft with more evidence; do not silently enable it.
- [#91](https://github.com/neusse/Edge-Scanner-NG/issues/91): Ordered candle-behavior engine remains design/TODO, not a live detector. The referenced design and inventory are now in `main`.

## Blockers and Risks
- The currently running scanner is from the older snapshot checkout, not published `main`. Do not replace files or restart it mid-session without a deliberate operational plan.
- `#84`–`#86` are not implemented by this merge. The PRD has acceptance boxes and live-session evidence still outstanding.
- Four tests in the running checkout could not acquire its single-instance lock while the live scanner ran; the integrated clean checkout passed all tests. This was an environment collision, not a test exemption.
- Dashboard lint has 5 non-fatal warnings, one in the new Schwab screener. Python reports 717 warnings, many from Pandas deprecations; these were not treated as failures.

## Decisions and Rationale
- Preserved the original running checkout and captured its working code on `codex/local-work-snapshot-20260928` before integration; avoids changing files under a live process.
- Did not publish `yfinance_earnings.py`: it is an untested, unintegrated scratch script with a separate dependency.
- Did not equate passing fixtures with live Schwab readiness. Provider acknowledgements, capacity behavior, reconnects, and a full session still need observation.

## Repo State Snapshot
- Clean checkout `main`: clean and pushed; no unpushed commits.
- Running checkout: snapshot branch plus untracked `yfinance_earnings.py`; no uncommitted tracked changes.

## Validation
- Build/typecheck: PASS — `npm run build` in `dashboard-v2`.
- Tests: PASS — `.\.venv\Scripts\python.exe -m pytest -q --disable-warnings` (990 passed, 6 skipped).
- Lint: PASS with warnings — `npm run lint` (0 errors, 5 warnings).
- Manual: GitHub `main` SHA verified against local; no post-merge live Schwab session was run.

## Open Questions
- When can the live scanner be stopped so the original checkout can switch to current `main` and receive an operational smoke test?
- Which measured Shadow-ranking thresholds should be chosen only after retained session data is reviewed?
- Do [#14](https://github.com/neusse/Edge-Scanner-NG/issues/14) and [#16](https://github.com/neusse/Edge-Scanner-NG/issues/16) still have unmet acceptance items? Both have related implementation in `main` but remain open; reconcile before closing.

## Resume Steps
1. In the clean checkout, `git fetch origin` and compare `git status --short --branch`, `git rev-parse HEAD`, and `git rev-parse origin/main` to this snapshot.
2. After the live session, plan the running checkout transition; preserve `yfinance_earnings.py`, stop the scanner intentionally, then switch that checkout to `main` and smoke-test the Schwab screener.
3. Capture one full live Observe/Manual session with acknowledgements, readiness, failure/reconnect, and retained-session review. Keep alerts disabled for unready candidates.
4. Implement #84 as a focused, fixture-tested Shadow-only change, then gather its required observation period before deciding on #85. Finish #86 afterward.
5. Separately triage #75, #91, #14, and #16; do not conflate trigger design with the Schwab subscription work.

## Quick Commands
```powershell
Set-Location 'C:\Users\georg\Documents\Codex\Edge-Scanner-NG'
git fetch origin
git status --short --branch
.\.venv\Scripts\python.exe -m pytest -q --disable-warnings
Set-Location .\dashboard-v2
npm run build
```

## Important Files
- `C:\Users\georg\Documents\Codex\Edge-Scanner-NG\docs\prds\schwab-dynamic-universe-v2.0-prd.md` — phase decisions and acceptance criteria.
- `C:\Users\georg\Documents\Codex\Edge-Scanner-NG\docs\plans\dynamic-schwab-discovery-plan.md` — implementation sequence.
- `C:\Users\georg\Documents\Codex\Edge-Scanner-NG\scanner\schwab_screener.py`, `C:\Users\georg\Documents\Codex\Edge-Scanner-NG\scanner\dynamic_universe.py`, `C:\Users\georg\Documents\Codex\Edge-Scanner-NG\scanner\dynamic_readiness.py` — discovery, membership, readiness boundaries.
- `C:\Users\georg\Documents\Codex\Edge-Scanner-NG\dashboard-v2\src\windows\schwabscreener\SchwabScreenerWindow.tsx` — Observe/Manual UI.
- `C:\Users\georg\Documents\Codex\Edge-Scanner-NG\docs\CANDLE_BEHAVIOR_ENGINE.md`, `C:\Users\georg\Documents\Codex\Edge-Scanner-NG\docs\CANDLE_BEHAVIOR_INVENTORY.md` — future ordered-pattern design.

## Machine Notes
- Windows/PowerShell. Clean checkout has a working `.venv` and `dashboard-v2/node_modules`.
- Original checkout has an active scanner process and single-instance lock; do not use it for lock-dependent tests while running.

## Change Log
- `2026-09-28T18:12:14Z` — reconciled unpublished local work with GitHub `main` and recorded next workstreams.
