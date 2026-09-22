# Edge Scanner


[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A real-time intraday stock scanner for US equities that runs on your own machine. It streams
1-minute bars for thousands of symbols, checks the setups you define on every bar, and shows the
alerts in a browser dashboard you lay out yourself. For day traders who want their own scans
instead of someone else's alert list.

- **Your machine, your keys.** No account, no subscription, no telemetry. Market data comes from
  your own Alpaca or Charles Schwab login, and everything it builds stays in `data/`. Any other
  provider can be added behind the same interface.
- **Setups without code.** Compose them in the dashboard from about 45 triggers and a list of
  conditions. Changes apply on the next bar, with no restart.
- **It tells you why.** For any stock, Setup Check shows what each setup did in the last few
  minutes and exactly which filter stopped an alert.
- **A dashboard you arrange.** Free-floating windows, several saved screens, linked symbols, and a
  WebSocket feed other programs can subscribe to.
- **Free and MIT licensed.** No paid tier.

**What this is not:** not financial advice, not a signal service, and not a broker. It never places
an order. The sample setups are starting points, not a strategy. The screenshots below come from a
replayed past session, so the alerts are synthetic in the sense that they were produced by a replay:
they are not anyone's real account, positions or orders.


## Privacy and your data

Edge Scanner is local-first. The scanner and the dashboard listen on `localhost` only, and there is
no login because nothing is exposed to the network.

- **Stays on your machine:** your API keys in `.env`, the setups, universe filters, watchlists and
  screens you build, the bar caches, and the alert archives. All of it under `data/`, which is
  gitignored.
- **Goes out, to your own accounts:** market data requests to Alpaca or Charles Schwab, signed with
  your keys. They see the symbols you scan, as any market-data client would.
- **Goes out, unauthenticated:** company profile fields from Yahoo Finance, and per-symbol news
  headlines from Yahoo Finance and Nasdaq RSS. When Schwab is the data provider, richer company
  fundamentals come from the authenticated Schwab Instruments endpoint. Those services see the
  symbol being requested and your IP. Turn the news ones off with `NEWS_RSS_SOURCES=` in `.env`.
- **Never collected:** no analytics, no crash reports, no usage data. The project has no server.

## What's inside

- **Custom setups, no code.** A catalog of about 45 triggers (candle patterns, level breaks,
  crosses, VWAP and EMA behaviour, opening range, momentum, relative strength against SPY), each
  with its own options and parameters. Combine them with AND, OR or "at least N of", add conditions
  the stock must meet, and pick the universe the setup runs on.
- **Universe filters.** Named screens over price, liquidity, ATR, float, sector, relative volume and
  more, deciding which stocks a setup may alert on.
- **Dashboard windows.** Alert tables, charts, rankings (gainers, losers, most active, pre-market
  lists, new highs and lows), a Yahoo-backed candidate screener, news, stock info, watchlists, Setup
  Check and a market clock. Screener results can be saved as named, described watchlists; one list can
  be selected as the scanner universe for the next start.
- **One data connection.** Everything runs in one process on one market-data websocket: Alpaca by
  default, or Charles Schwab, which is free with a brokerage account. Providers sit behind one
  `DataFeed` interface, so anything that can stream 1-minute bars and answer for history can be
  plugged in: see [adding a data provider](USER_GUIDE.md#adding-a-data-provider).
- **A feed for other programs.** `ws://localhost:7777/ws/alerts`, with server-side filters by
  source, setup or symbol.
- **Extensible.** Optional engine plugins can add built-in setups and data providers
  (`scanner/plugins.py`).

## Screenshots

**Price Action**: alerts on the left, the one you clicked pinned above the table and marked on the
intraday chart, a daily chart and news on the right, and a second link group along the bottom where
rankings drive their own chart.

![The Price Action screen: an alert table with the selected alert pinned above it, an intraday chart marking the alert bar, a daily chart, news, and rankings driving a second chart](docs/screenshot-price-action.png)

**Pre-Market**: pre-market gainers, losers and volume leaders. Click any symbol and the chart, the
daily chart, news and stock info all follow it.

![The Pre-Market screen showing three pre-market ranking tables on the left, with a chart, a daily chart, news and stock info following the selected symbol](docs/screenshot-pre-market.png)

**Config**: every setup in one list, with how it fires, when it may fire, and the universe filter it
runs on. Alerts, parameters, a plain-English summary and a per-stock check are the other tabs.

![The Config window: the setup list on the left, and on the right the selected setup's alert mode, direction, sessions, notes and universe filter](docs/screenshot-setup-builder.png)

## Requirements

- Python 3.11 or newer, and Node.js 20 or newer to build the dashboard
- Market data, either:
  - an [Alpaca](https://alpaca.markets) account (paper is fine). The default `ALPACA_FEED=sip`
    needs their paid market-data plan. The free `ALPACA_FEED=iex` works, but it is one exchange, so
    volume and relative volume read far lower and volume-based setups fire much less; or
  - a Charles Schwab brokerage account, free, using `DATA_PROVIDER=schwab`. It covers the whole
    universe: real 1-minute bars for the 300 most liquid symbols (Schwab's limit) and bars built from
    Schwab's quotes for the rest, which are close but not identical. See the
    [user guide](USER_GUIDE.md#step-3-add-your-alpaca-keys) for the one-time login, which Schwab
    expires every 7 days.

Those two ship because they are the two the project runs on and tests against. Another provider is
one class and one line in a registry: [adding a data provider](USER_GUIDE.md#adding-a-data-provider).

Tested on Windows. macOS and Linux should work through `start_scanner.sh`.

## Quick start

On Windows, clone the repository and run `setup.bat`. It checks your Python and Node versions,
installs everything, builds the dashboard and creates `.env`. Then add your keys to `.env` and run
`start_scanner.bat`.

By hand, or on macOS and Linux:

```bash
git clone https://github.com/simonro/edge-scanner.git
cd edge-scanner
python -m venv .venv
```

Activate the environment (`.venv\Scripts\activate` on Windows, `source .venv/bin/activate`
elsewhere), then:

```bash
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and set `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` and `ALPACA_FEED`, or set
`DATA_PROVIDER=schwab` and the Schwab keys.

Build the dashboard once:

```bash
npm --prefix dashboard-v2 install
npm --prefix dashboard-v2 run build
```

Run it (`start_scanner.bat` on Windows):

```bash
./start_scanner.sh
```

Open http://localhost:7777.

**Every install starts empty.** No watchlists, no alert history and none of anyone else's setups.
You get two sample screens (Pre-Market and Price Action) so the dashboard is not a blank page. The
first start downloads a year of daily bars and 20 days of 5-minute bars for the whole universe,
which takes 10 to 20 minutes; later starts use the cache in `data/`.

Optional, to start from working examples instead of an empty setup list:

```bash
python scripts/install_setup_library.py
```

The full guide is [USER_GUIDE.md](USER_GUIDE.md).

## Updating to a new release

```bash
git pull
pip install -r requirements.txt
npm --prefix dashboard-v2 install
npm --prefix dashboard-v2 run build
```

Your `.env` and everything in `data/` (setups, universe filters, screens, watchlists, caches,
alert archives) are left alone. Release notes call out anything that needs a one-time step.

## Configuration

`.env` keys, all optional unless you use the feature:

| Key | Default | What it does |
|---|---|---|
| `ALPACA_API_KEY`, `ALPACA_SECRET_KEY` | none | Alpaca market data and Benzinga news |
| `ALPACA_FEED` | `sip` | `sip` (paid, consolidated tape) or `iex` (free, one exchange) |
| `DATA_PROVIDER` | `alpaca` | `alpaca`, `schwab`, or a provider you add yourself |
| `SCHWAB_APP_KEY`, `SCHWAB_APP_SECRET`, `SCHWAB_CALLBACK_URL` | none | Charles Schwab market data |
| `NEWS_RSS_SOURCES` | `yahoo,nasdaq` | Free per-symbol news feeds. Empty turns them off |

## Layout

| Path | What |
|---|---|
| `scanner/` | the engine: data providers, per-symbol state, indicators, setups, universe filters, API |
| `scanner/trigger_catalog.py` | every trigger a custom setup can use |
| `scanner/conditions.py` | the conditions setups and universe filters can check |
| `scanner/data/` | market-data providers (`alpaca.py`, `schwab.py`) behind one interface |
| `scanner/custom_setups_defaults.json` | sample setups, seeded on first run |
| `scanner/setup_library.json` | the setup library (`scripts/install_setup_library.py`) |
| `scanner/plugins.py` | extension points for optional engine plugins |
| `scripts/run_live.py` | the live scanner |
| `dashboard-v2/` | the React dashboard |
| `data/` | runtime data: caches, settings, your setups, alert archives (gitignored) |

## Development

```bash
python -m pytest
npm --prefix dashboard-v2 run lint
npm --prefix dashboard-v2 run dev
```

`npm run dev` serves the dashboard with hot reload on http://localhost:5174 and proxies the API to a
running scanner on 7777.

## Contributing

Bug reports, new triggers and pull requests are welcome. [CONTRIBUTING.md](CONTRIBUTING.md) covers
the setup, the tests a change needs, and how to add a trigger, a condition or a data provider.
Please report security problems privately, as described in [SECURITY.md](SECURITY.md), not in a
public issue.

## Who made this / Not me,  this guy made it.  This is my Fork.  

A day trader who wanted scans that match how he actually trades, and a dashboard that fits one
ultrawide monitor. Built by describing the problems to Claude Code.

- YouTube: [@tapetoedge](https://www.youtube.com/@tapetoedge)
- X: [@tapetoedge](https://x.com/tapetoedge)

Educational software, not financial advice. No affiliate relationship with Alpaca, Schwab or
anything else shown here. You are responsible for your own trading decisions, and the software
comes with no warranty.

## License

MIT, see [LICENSE](LICENSE). Third-party notices, including the TradingView Lightweight Charts
attribution, are in [NOTICE](NOTICE).
