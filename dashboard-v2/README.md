# Edge Scanner Dashboard

The web UI of Edge Scanner: a desktop-style, multi-window dashboard for the scanner engine. Windows float freely inside a fixed
workspace: drag, resize, stack and maximize them, link them by color so a click on a symbol in one
drives the others, and save the arrangement as named screens.

Built with React, TypeScript, Vite and zustand. Charts use
[Lightweight Charts](https://github.com/tradingview/lightweight-charts) by TradingView
(Apache-2.0); keep its attribution logo enabled.

## Develop

Requires Node.js 20+ and a running scanner (the Python backend) on port 7777.

```bash
cd dashboard-v2
npm install
npm run dev        # Vite on http://localhost:5174/v2/, proxies /api to http://localhost:7777
```

The dev server proxies REST calls (`/api/*`) to the scanner. The alert WebSocket connects directly
to `ws://<host>:7777/ws/alerts`.

## Build

```bash
npm run build      # type-checks, then writes dashboard-v2/dist
```

The scanner serves `dashboard-v2/dist` at **http://localhost:7777/v2/** and reads it from disk per
request, so a rebuild shows up on reload without restarting the scanner.

Other scripts: `npm run lint` (ESLint), `npm run preview` (serve the built bundle with Vite).

## Windows

| Window | Data | Notes |
|---|---|---|
| Scanner | unified alert feed `ws://<host>:7777/ws/alerts` | per-window source, setup, direction, score and symbol filters; column picker; resizable columns; row tint; sound and text-to-speech |
| Chart | `/api/bars` | intraday and daily timeframes, extended hours, VWAP, EMAs, daily SMAs, prior-day and premarket levels |
| Rankings | `/api/v2/toplists`, `/api/premarket`, `/api/v2/events` | RVOL leaders, gainers and losers, 5-min movers, premarket lists, new HOD / LOD stream |
| Screener | `/api/v2/screener/yahoo` | Yahoo candidate discovery with preset and custom filters; saves snapshots to watchlists |
| News | `/api/v2/news` | market-wide or following the linked symbol |
| Stock Info | `/api/v2/state`, `/api/v2/fundamentals` | live state plus Yahoo profile data and, on the Schwab feed, the full Instruments fundamental record |
| Watchlist | `/api/v2/watchlists`, `/api/v2/snapshot` | editable lists with live columns |
| Clock | `/api/v2/clock` | ET clock, session phase, market regime, SPY, feed health |
| Setup check | `/api/v2/check/{symbol}` | what every setup did on one symbol in the last few minutes, and why |

One watchlist can be assigned through `/api/v2/universe/selection` as the universe for the next scanner
start. The selection is explicit, never unions multiple lists, and is limited to 288 user symbols so SPY
and eleven sector ETFs fit within Schwab's 300 chart-stream limit. Watchlists support descriptions,
provenance metadata, and CSV/text import and export.

## Alert sources

Every alert on the feed carries a `source`:

- `custom`: setups you compose in Config from the trigger catalog (ids `cs_*`).
- `system`: built-in setups provided by an optional engine plugin. A backend without one reports
  none, and the dashboard then shows no built-in setups and no `system` source anywhere.

The dashboard opens one WebSocket for the whole app and each Scanner window filters client-side.

## Capabilities

At startup the dashboard reads `GET /api/v2/capabilities` (a map of flags; a failed request
counts as all false) and shows optional UI only when the backend has it:

- `system_setups`: built-in setups from an engine plugin, in Config, the Scanner setup filter
  and the `system` source. Their codes, names and directions come from `GET /api/v2/setups`
  (`system`), never from the dashboard itself.
- UI plugins: any module in `src/plugins/` that exports `symbolActions` adds per-symbol
  buttons next to every ticker (see `src/plugins.ts`). None ship by default.

## Config

`Ctrl+,` opens the Config panel with three sections:

- **Setups**: built-in (when the backend provides them) and custom setups in one list. Built-in
  setups can be renamed (display name only; the setup code on the feed never changes), have their
  thresholds tuned, and be duplicated as custom setups. Custom setups combine alerts from the trigger catalog with OR, AND or
  AT LEAST logic, plus per-setup parameters and a universe filter.
- **Rankings**: which universe filter each ranked list uses and how many rows it opens with.
- **Universe**: named symbol filters that setups and rankings point at.

Changes apply on the next bar without a restart.

## Screens and persistence

- **Screens** (layouts) are saved on the server through `/api/v2/layouts`, 750 ms after any
  change. Window bounds are stored as fractions of the workspace, so a screen keeps its
  arrangement on any monitor size or scaling factor. Older screens are migrated on load.
- Two screens ship in `defaults/screens/` and are seeded on first run when no screens exist:
  **Pre-Market** (pre-market gainers, losers and volume rankings driving a pre-market chart, a
  daily chart, news and stock info) and **Price Action** (an Alerts scanner beside intraday and
  daily charts and news, plus a second link group: RVOL leaders and 5-min movers driving a
  1-minute chart). Both stay available under Screens > Starter layouts.
- Per-browser preferences (active screen, theme, hidden menu, global mute) live in `localStorage`.

## Shortcuts

`Ctrl+K` add window, `Ctrl+L` lock / unlock the layout, `Ctrl+,` Config, double-click a title bar
to maximize / restore, hold `Alt` while dragging to disable snapping, `Esc` closes menus and
dialogs.

## Code layout

- `src/components/`: window manager (`Workspace.tsx`), top bar, Config panel and shared widgets.
- `src/windows/`: one folder per window type, plus `registry.ts` and `defaults.ts`.
- `src/stores/`: zustand stores (feed, screens, setups, settings, profiles and more).
- `src/lib/`: API client, WebSocket client, formatting, audio.
- `src/index.css`: design tokens. It is the only file allowed to contain color literals; two
  themes ship (`navy`, the dark default, and `daylight`).

## License

MIT
