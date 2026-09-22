# Native trigger contract matrix

This table is the event-lifetime and boundary reference for the 45 native alert triggers. The
catalog exposes `lifetime` and `sessions` to the setup editor and Setup Check. All triggers use
US Eastern session dates. A bar timestamp marks its start; completed-candle events are evaluated
on the next incoming minute. Unless a row says otherwise, a crossing is strict on the current
side and equality belongs to the previous/inside side. The setup's own repeat timer and the feed
sink can further suppress publication; those do not change the trigger's event contract.

| Trigger id | Lifetime | Allowed sessions | Decisive boundary / re-arm |
|---|---|---|---|
| `hod` | new_extreme | RTH | New strict high/low beyond the prior regular-session extreme; later genuine extremes may fire. |
| `hod_ext` | new_extreme | Pre, RTH | New strict high/low beyond the prior extended-session extreme. |
| `near_hod` | approach_edge | RTH | Close inside one 1m ATR of prior HOD/LOD, bar high/low not through it; re-arms after leaving approach. |
| `hi_lo_60d` | first_breach_per_day | RTH | First strict wick beyond N completed daily highs/lows per side/day; replayed breach latches. |
| `hi_lo_52w` | first_breach_per_day | RTH | First strict wick beyond up to 252 completed daily highs/lows per side/day. |
| `prior_day_break` | first_breach_per_day | Pre, RTH | First strict wick beyond yesterday's high/low per side/day. |
| `pm_break` | first_breach_per_day | RTH | First strict wick beyond established premarket high/low per side/day. |
| `new_candle_high` | once_per_candle | Pre, RTH | Current candle high strictly exceeds prior N completed candle highs. |
| `new_candle_low` | once_per_candle | Pre, RTH | Current candle low strictly undercuts prior N completed candle lows. |
| `break_recent_high` | once_per_candle | Pre, RTH | Price close crosses above stored swing high; no second alert in the same candle. |
| `break_recent_low` | once_per_candle | Pre, RTH | Price close crosses below stored swing low; no second alert in the same candle. |
| `near_last_high` | approach_edge | Pre, RTH | Close approaches swing high inside one timeframe ATR; current high remains at/below level. |
| `near_last_low` | approach_edge | Pre, RTH | Close approaches swing low inside one timeframe ATR; current low remains at/above level. |
| `reject_last_high` | completed_candle | RTH | Completed candle touches/pokes prior swing high, closes red back below it. |
| `reject_last_low` | completed_candle | RTH | Completed candle touches/pokes prior swing low, closes green back above it. |
| `orb_breakout` | once_per_day | RTH | After today's opening candle completes, close crosses its high once. |
| `orb_breakdown` | once_per_day | RTH | After today's opening candle completes, close crosses its low once. |
| `bull_candle_close` | completed_candle | Pre, RTH | Every completed green candle qualifies. |
| `bear_candle_close` | completed_candle | Pre, RTH | Every completed red candle qualifies. |
| `bull_engulfing` | completed_candle | Pre, RTH | Green body strictly larger than and enveloping prior red body. |
| `bear_engulfing` | completed_candle | Pre, RTH | Red body strictly larger than and enveloping prior green body. |
| `bull_harami` | completed_candle | Pre, RTH | Smaller green body inside prior red body. |
| `bear_harami` | completed_candle | Pre, RTH | Smaller red body inside prior green body. |
| `doji` | completed_candle | Pre, RTH | Body/range at or below configured percentage; zero range excluded. |
| `inside_bar` | completed_candle | Pre, RTH | High strictly below and low strictly above previous candle's bounds. |
| `double_inside_bar` | completed_candle | Pre, RTH | Two consecutive strict inside bars. |
| `upper_shadow` | completed_candle | Pre, RTH | Upper tail/body at or above configured ratio. |
| `lower_shadow` | completed_candle | Pre, RTH | Lower tail/body at or above configured ratio. |
| `volume_spike` | completed_candle | Pre, RTH | Completed volume/prior average at or above configured ratio. |
| `consec_candles` | streak_edge | Pre, RTH | Fires when completed same-color streak first reaches N; opposite candle re-arms. |
| `cross_above` | recross | Pre, RTH | Prior close at/below and current close strictly above selected level; higher timeframe uses completed candles. |
| `cross_below` | recross | Pre, RTH | Prior close at/above and current close strictly below selected level; higher timeframe uses completed candles. |
| `vwap_v` | completed_candle | Pre, RTH | Completed touch candle snaps back from distance with bounded prior dwell. |
| `range_break` | range_exit_edge | Pre, RTH | Close exits tight N-candle range on qualifying per-minute volume; re-arms on return inside. |
| `ema_cross_ema` | recross | Pre, RTH | Fast/slow EMA strict cross after both are warmed on completed candles. |
| `through_vwap` | recross | RTH | Close crosses VWAP on a 1m candle meeting range/average multiplier. |
| `vwap_support` | completed_candle | RTH | Completed green candle's low in bounded VWAP touch band, close above. |
| `vwap_resistance` | completed_candle | RTH | Completed red candle's high in bounded VWAP touch band, close below. |
| `back_to_ema` | once_per_candle | Pre, RTH | N completed candles away from their own EMA, then current touch/hold; once per current candle. |
| `running` | qualifying_bar | Pre, RTH | Each 1m close-to-close move meeting threshold qualifies; setup cooldown limits repeats. |
| `pct_change` | recross | Pre, RTH | Crosses configured percent from prior close; equality qualifies on arrival. |
| `gap` | once_per_day | RTH | Session open exceeds configured gap magnitude; first qualifying evaluation per side/day. |
| `rvol_cross` | recross | RTH | RVOL crosses from below to at/above threshold; falls below to re-arm. |
| `rs_spy` | recross | RTH | Stock minus SPY 15m momentum crosses relative-strength threshold. |
| `momentum_burst` | qualifying_bar | RTH | Each 1m bar meeting ATR range and directional close-position thresholds qualifies. |

The executable cases in `tests/test_custom_setups.py`, `tests/test_trigger_contracts.py`, and
`tests/test_live_scanner.py` cover event families, strict/equality boundaries, session filtering,
restart priming, parameter-distinct instances, Setup Check levels/notes, and emitted evidence.
Pass-through `setup:*` triggers are not native; their event lifetime belongs to the producing
system setup and is reported as `upstream_setup`.
