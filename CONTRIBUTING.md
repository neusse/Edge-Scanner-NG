# Contributing to Edge Scanner NG

Thanks for helping. Bug reports, fixes and features are all welcome. This page covers how to set
up, what to test, and what a pull request needs before it can be merged.

**Security problems:** please do not open a public issue. Follow [SECURITY.md](SECURITY.md) and use
GitHub's private vulnerability reporting.

## What the project cares about

These decide most review questions, so they are worth knowing before you start:

1. **Local first.** Everything runs on your machine, against your own broker keys. No accounts, no
   telemetry, no cloud service. The API and the dashboard listen on localhost. A change that sends
   data somewhere new, binds beyond localhost, or adds a background job needs an issue first.
2. **One market-data connection.** Alpaca allows one market-data websocket per account, so the whole
   process shares a single stream through `scanner/data/`. Nothing may open a second one.
3. **The evaluator never looks ahead.** A trigger or condition sees only bars that have closed and
   the state built from them. A change that reads a later bar, or today's daily bar, is a bug even
   when it makes a backtest look better.
4. **No code to add a setup.** Setups are composed in the dashboard from the trigger catalog and the
   condition list. New behavior belongs in `scanner/trigger_catalog.py` or `scanner/conditions.py`
   as a reusable piece, not as a hardcoded strategy.
5. **Saved work survives.** Screens, setups, universe filters and alert archives written by an older
   version must keep loading. Migrate on load instead of rewriting or dropping what a user has.
6. **Small and focused.** One pull request, one change. Large pull requests that bundle several
   features will usually be asked to split.

## Setting up

Requirements: Python 3.11 or newer, Node.js 20 or newer, and an Alpaca account (paper is fine) or a
Charles Schwab brokerage account for market data.

On Windows, run `setup.bat` once, then `start_scanner.bat`. By hand:

```bash
python -m venv .venv
.venv\Scripts\activate            # source .venv/bin/activate elsewhere
pip install -r requirements.txt
cp .env.example .env              # then fill in your keys
npm --prefix dashboard-v2 install
npm --prefix dashboard-v2 run build
./start_scanner.sh                # start_scanner.bat on Windows
```

A new install starts empty: no watchlists, no screens of yours, no alert history. It seeds two
sample screens and, if you run `python scripts/install_setup_library.py`, the sample setups. The
first start downloads market history into `data/`, which can take 10 to 20 minutes.

**Never use or commit real personal data**: not in tests, fixtures, screenshots or issues. Market
data itself is fine; account numbers, keys and statements are not.

## Tests

CI runs these on every pull request, and they must pass:

```bash
python -m pytest
npm --prefix dashboard-v2 run lint
npm --prefix dashboard-v2 run build
```

What to add:

- **A fix:** a test that fails without it.
- **A trigger, condition or indicator:** a test with hand-built bars and the expected outcome
  spelled out, including the bar where it must NOT fire. `tests/test_triggers.py` and
  `tests/test_conditions.py` show the pattern.
- **An API change:** a test in `tests/test_api_v2.py`.
- **A dashboard change:** say how you checked it, and attach a screenshot in the pull request.

Tests must not reach the network. Feeds and fundamentals are injected, so pass a fake instead.

## Adding a trigger or a condition

A **trigger** is the event that fires an alert. Add a `TriggerDef` to `scanner/trigger_catalog.py`
with its name, category, description, direction, options and parameters, then the evaluator function
under `@_impl("<id>")`. It receives the evaluation context and returns a `Fire` or `None`. Keep it
pure: same state and bar in, same answer out.

A **condition** is a filter a setup or a universe filter checks. Add a `ConditionDef` to
`scanner/conditions.py` with its unit, default operator and default value, and a resolver that reads
the value off the state. Say in the description whether it is static (universe membership) or
dynamic (checked when a setup would fire).

Both appear in the dashboard automatically: the catalog is the API. Do not hardcode anything about
them in the frontend.

## Adding a data provider

Implement the `DataFeed` interface in `scanner/data/interface.py` and register the class in
`scanner/data/__init__.py`. `scanner/data/schwab.py` is a worked example, including its own parquet
cache folder so two providers can never mix their bars. A provider must be selectable with
`DATA_PROVIDER` and must not change any signal code. The user guide has the full checklist under
[Adding a data provider](USER_GUIDE.md#adding-a-data-provider), including the parity run every new
provider should pass before anyone trades on it.

Only Alpaca and Schwab ship, because those are the two that are tested against live data. A pull
request adding another is welcome, and should say what was verified: a session of live bars, and a
history comparison against a provider that is already supported.

## Pull requests

- Branch from `main` and keep the change focused.
- Describe what changed and why, and how you tested it.
- Say so if the change adds a dependency, a network call, file access outside the app folder, or a
  change to saved-file formats. New dependencies need a reason.
- Never include `.env` files, API keys, `data/` contents or account details.

## Code style

Match the code around you. Comments explain why something is done, not what the next line does.
Keep user-facing text plain and specific: say what happened and what to do next. No em dashes.

## Questions

Ask in [Discussions](https://github.com/neusse/Edge-Scanner-NG/discussions). Never paste real account
details or API keys.
