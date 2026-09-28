# Candle-behavior vocabulary inventory

Status: **research inventory; names are not implemented detectors**

This inventory captures the `candle_behavior` sections in the 17 validated
Markdown strategy documents found in the sibling Z-Trading worktree on
2026-09-26. The observed repository base was
`456d12d05cc075b22f41efe02e09cf43d01bdf40`; the worktree was dirty outside the
source strategy documents.

The terms below are declarations of intent. Z-Trading currently preserves them in
a compiled catalog but does not execute their ordered behavior. See the proposed
[ordered candle-behavior engine](CANDLE_BEHAVIOR_ENGINE.md) for the Edge design and
safety requirements.

## Strategy-by-strategy inventory

| Strategy | Required patterns | Confirmation | Invalidation |
| --- | --- | --- | --- |
| Breakout | `range_consolidation`, `breakout_close` | `volume_expansion`, `hold_above_breakout_level` | `close_back_inside_range`, `failed_retest` |
| Dividend Capture | `stable_pre_ex_date_trend` | `support_hold`, `low_volatility` | `support_break`, `negative_dividend_news` |
| Earnings Event | `post_event_range`, `directional_reaction` | `vwap_hold`, `volume_expansion` | `earnings_gap_reversal`, `failed_vwap_hold` |
| Gap Trading | `gap_open`, `opening_range_definition` | `opening_range_break`, `vwap_hold` | `gap_failure`, `opening_range_reversal` |
| High-Yield ETF Income Rotation | `stable_trend_or_base` | `price_above_sma100_or_reclaim` | `distribution_breakdown`, `price_below_sma100_with_followthrough` |
| Market Making | `stable_intraday_range` | `spread_stable`, `book_size_stable` | `volatility_spike`, `news_spike`, `spread_widening` |
| Mean Reversion | `lower_wick_rejection`, `close_back_inside_band` | `decelerating_sell_volume`, `bullish_reversal_candle` | `close_below_reversal_low`, `support_break_on_volume` |
| Momentum | `higher_high`, `higher_low`, `close_near_high` | `volume_above_average`, `close_above_sma20` | `close_below_sma50`, `high_volume_reversal` |
| News-Based Trading | `news_range_break`, `vwap_hold` | `volume_expansion`, `close_above_vwap_for_long` | `failed_news_breakout`, `reversal_below_vwap` |
| Options Premium | `range_bound_or_moderate_trend` | `support_resistance_valid`, `no_event_breakout` | `range_break_against_position`, `event_risk_detected` |
| Pullback | `orderly_pullback`, `higher_low` | `reversal_close`, `support_hold` | `close_below_support`, `close_below_sma50` |
| Sector Rotation | `sector_breakout_or_pullback` | `stock_confirms_sector_strength` | `sector_relative_strength_break` |
| Statistical Arbitrage | `spread_extension_without_news` | `spread_stalls_at_extreme`, `liquidity_stable` | `correlation_break`, `news_driven_divergence` |
| Swing Trading | `support_retest_or_breakout` | `close_above_trigger_level`, `volume_confirmation` | `close_below_setup_low`, `failed_breakout` |
| Trend Following | `higher_highs_higher_lows` | `close_above_sma50`, `sma50_above_sma200` | `close_below_sma50_with_followthrough`, `lower_low` |
| Value Fundamental | `base_or_reversal` | `close_above_sma50_or_support_reclaim` | `support_break`, `negative_fundamental_update` |
| Volatility Breakout | `range_compression`, `breakout_close` | `wide_range_candle`, `volume_expansion` | `close_back_inside_range`, `breakout_failure_on_volume` |

## Normalized terms by role

The lists retain every unique term so later work can trace a proposed primitive
back to the source vocabulary.

### Required-pattern terms

`base_or_reversal`, `breakout_close`, `close_back_inside_band`,
`close_near_high`, `directional_reaction`, `gap_open`, `higher_high`,
`higher_highs_higher_lows`, `higher_low`, `lower_wick_rejection`,
`news_range_break`, `opening_range_definition`, `orderly_pullback`,
`post_event_range`, `range_bound_or_moderate_trend`, `range_compression`,
`range_consolidation`, `sector_breakout_or_pullback`,
`spread_extension_without_news`, `stable_intraday_range`,
`stable_pre_ex_date_trend`, `stable_trend_or_base`,
`support_retest_or_breakout`, `vwap_hold`.

### Confirmation terms

`book_size_stable`, `bullish_reversal_candle`, `close_above_sma20`,
`close_above_sma50`, `close_above_sma50_or_support_reclaim`,
`close_above_trigger_level`, `close_above_vwap_for_long`,
`decelerating_sell_volume`, `hold_above_breakout_level`, `liquidity_stable`,
`low_volatility`, `no_event_breakout`, `opening_range_break`,
`price_above_sma100_or_reclaim`, `reversal_close`, `sma50_above_sma200`,
`spread_stable`, `spread_stalls_at_extreme`,
`stock_confirms_sector_strength`, `support_hold`, `support_resistance_valid`,
`volume_above_average`, `volume_confirmation`, `volume_expansion`, `vwap_hold`,
`wide_range_candle`.

### Invalidation terms

`breakout_failure_on_volume`, `close_back_inside_range`,
`close_below_reversal_low`, `close_below_setup_low`, `close_below_sma50`,
`close_below_sma50_with_followthrough`, `close_below_support`,
`correlation_break`, `distribution_breakdown`, `earnings_gap_reversal`,
`event_risk_detected`, `failed_breakout`, `failed_news_breakout`,
`failed_retest`, `failed_vwap_hold`, `gap_failure`, `high_volume_reversal`,
`lower_low`, `negative_dividend_news`, `negative_fundamental_update`,
`news_driven_divergence`, `news_spike`, `opening_range_reversal`,
`price_below_sma100_with_followthrough`, `range_break_against_position`,
`reversal_below_vwap`, `sector_relative_strength_break`, `spread_widening`,
`support_break`, `support_break_on_volume`, `volatility_spike`.

## Current Edge overlap

| Vocabulary group | Current Edge building blocks | Remaining gap |
| --- | --- | --- |
| Range consolidation and breakout close | `range_break`, recent-high/low breaks | Current range trigger emits immediately; no separately captured frozen range or later confirmation. |
| True compression | Tight Range Breakout only | No progressive range contraction, declining participation, or falling volatility requirement. Tight range must not be renamed compression. |
| Engulfing, harami, doji and reversal candles | Candle triggers for engulfing, harami, doji, upper/lower shadow | Shape exists, but trend/location context and ordered follow-through do not. |
| Lower-wick rejection | `lower_shadow`, reject-last-low, VWAP support | No generic “wick at captured support, then confirm” behavior. |
| Higher highs and higher lows | New-candle extremes and recent-high/low breaks | No ordered swing-structure detector that captures and compares successive pivots. |
| Volume expansion and confirmation | `volume_spike`, candle-relative volume, time-of-day RVOL | Available arithmetic, but not a reusable confirmation stage tied to a pending behavior. |
| Wide-range candle | `momentum_burst` and `running` provide partial range/move concepts | Needs a completed-candle range/ATR or range/baseline contract and close-location requirement. |
| Hold above breakout level | None generically | Requires a frozen level, subsequent completed closes, expiry, and invalidation. |
| Retest, failed retest, and close back inside | Failed Swing Break is one specialized ordered detector | Requires generic captured-level retest/hold/failure primitives. |
| VWAP hold or failure | VWAP support/resistance, V off VWAP, cross and current-state checks | Needs ordered hold/failure semantics when used after a different stage. |
| Opening range | ORB and ORB trade-cross | Definition exists; later hold, reversal, and failure stages are missing. |
| Gap behavior | Gap trigger and gap-related setup ingredients | Opening gap can be observed; ordered gap hold/failure/reversal needs behavior state. |
| SMA/VWAP level state | Classic SMA checks plus price/level crosses | Mostly composable as checks, but `with_followthrough`, reclaim, and later failure require ordering. |
| Support/base/pullback structure | EMA pullback, back-to-EMA, swing and VWAP primitives | Generic support capture, orderly pullback, higher-low, base quality, and retest sequences are missing. |
| Sector-relative confirmation | Sector-relative-strength checks | Current-state confirmation exists; persistent break/hold semantics need a behavior stage. |

## Terms that are not candle-only detectors

These terms may be valuable elsewhere, but a candle-behavior engine must not claim
to derive them from OHLCV bars alone.

### Event, news, and fundamental inputs

`post_event_range`, `news_range_break`, `stable_pre_ex_date_trend`,
`directional_reaction`, `no_event_breakout`, `event_risk_detected`,
`earnings_gap_reversal`, `failed_news_breakout`, `negative_dividend_news`,
`negative_fundamental_update`, `news_driven_divergence`, and `news_spike` require
fresh event, calendar, news, or fundamental evidence in addition to price bars.

### Quote, depth, liquidity, and cross-instrument inputs

`book_size_stable`, `spread_stable`, `spread_widening`, `liquidity_stable`,
`spread_extension_without_news`, `spread_stalls_at_extreme`, and
`correlation_break` require quote/depth or multiple-instrument state. They should
not be implemented as candle-pattern aliases.

### Position-aware terms

`range_break_against_position` and some support/failure terms become meaningful
only relative to a held position. Edge may publish the underlying market event,
but Trader owns the position-aware interpretation.

## Recommended first vertical slice

Start with one disabled long-only behavior:

1. `range_consolidation` captures the high and low from prior completed,
   contiguous five-minute candles.
2. `breakout_close` requires a later completed five-minute close above the frozen
   high.
3. `volume_expansion` qualifies that breakout using an explicit prior-only
   baseline.
4. `hold_above_breakout_level` requires a later completed close above the same
   level within two candles.
5. `close_back_inside_range` or `failed_retest` invalidates the pending behavior.
6. Edge emits only at confirmation and records every stage as evidence.

This slice exercises ordering, capture, confirmation, expiry, and invalidation
without depending on news, depth, positions, or external strategy state.
