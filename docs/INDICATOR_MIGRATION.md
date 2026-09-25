# Pandas TA Classic cutover

The scanner pins `pandas-ta-classic==0.8.32` in `requirements.txt`. Scanner,
replay, and chart overlays use `scanner/indicators/classic.py` for native SMA,
EMA, ATR, ADX, VWAP, MACD, RSI, Stochastic, CCI, Bollinger Bands, and OBV.
Optional TA-Lib acceleration is disabled so one calculation path is used.

Edge-specific time-of-day RVOL, volatility-adjusted relative strength versus
SPY/sector, and chart quality remain Edge calculations. Setup composition,
trigger edges, scoring, and session selection also remain Edge logic. The
Classic module owns native values; it does not define a trading strategy.

## User-visible behavior

- Config → Setups → Parameters offers each Classic study as a completed-candle
  check, including MACD and Bollinger components. Missing warm-up values block.
- The trigger picker offers completed-candle level crossings for MACD histogram,
  RSI, Stochastic %K, CCI, Bollinger %B, and ADX. A crossing is an event, unlike
  a check that remains true while a value is beyond its threshold. No saved
  setup is changed automatically.
- The chart fetches EMA/SMA/VWAP overlay values calculated by Edge. The browser
  no longer calculates those studies. `GET /api/bars/{symbol}/{timeframe}` can
  include `with_indicators=true`; `extended=true` selects extended-session
  EMA/SMA context, while VWAP remains regular-session anchored. Current-session
  VWAP on every chart timeframe is sampled from Edge's stored 1-minute bars,
  not recomputed from coarser chart candles.
- New alerts carry `indicator_calculation_version`. Their effective
  `detector_revision` includes that version. It is not a complete application
  build or data-source identity.

## Timing and input rules

Only completed candles feed alert studies. The live five-minute EMA tracker
and custom timeframe candle series consume a candle when the next slot begins;
the chart suppresses overlay points on an unfinished intraday candle. Regular-
session VWAP resets each ET day and excludes extended hours. A missing OHLCV
input breaks a study into complete contiguous segments so a value cannot
silently bridge a missing candle. Classic's own warm-up rules then apply.

The cutover intentionally changes some early indicator values. On a frozen WFC
2026-09-24 replay input, pre-cutover SMA50, EMA8, and RTH VWAP matched Classic
to floating-point precision. Old ATR14 differed by up to 0.0114 and old ADX14
by up to 0.490 during warm-up; Classic ATR14 first became valid one daily bar
later. These are reasons to revalidate alert thresholds, not evidence of a
trading edge. The current comparison command now checks the active compatibility
routes against Classic, not the removed handwritten implementation:

```powershell
.\.venv\Scripts\python.exe -m scripts.audit_classic_indicator_parity --input data\replay\inputs\2026-09-24-533531c62429 --symbol WFC
```

That input is a local frozen capture, not a repository fixture. The regression
suite tests prefix/no-lookahead calculations, selectable checks, an actual RSI
cross event, chart overlay values, and existing alert contracts. Broader
historical signal quality and live-market behavior still require observation;
none of these tests establishes that a setup is profitable or suitable to trade.

Library references: [Pandas TA Classic package](https://pypi.org/project/pandas-ta-classic/)
and [indicator catalog](https://xgboosted.github.io/pandas-ta-classic/indicators.html).
