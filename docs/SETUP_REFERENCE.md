# Alert and setup reference

Edge's **Config → Setups** window is the source of truth for the rules currently running. Each
setup combines triggers (the events), optional checks (conditions that must be true when it
fires), and a universe filter (eligible symbols). Open a setup there to see its exact thresholds,
enabled state, and description. Changes take effect on the next bar; they do not create alerts
for moves that already happened.

This page describes the saved setups in this checkout as of September 25, 2026. The
23 shipped definitions in `scanner/custom_setups_defaults.json` seed a new install
only when `data/setups/custom/` is empty; they never overwrite existing saved setups.
The live Config window may therefore differ. These are *watch alerts*, not trade
recommendations or orders.

## Momentum Watch

**Momentum Watch** is a long-side, regular-session watch alert. It requires at least **two** of
these three events for the same symbol within **15 minutes**:

1. A completed 3-minute candle bounces from VWAP support.
2. EMA(3) crosses above EMA(9) on completed 5-minute candles.
3. Price breaks upward from a tight five-candle, 5-minute range on qualifying one-minute volume.

At alert time, time-of-day relative volume must be at least 1.0, 5-minute relative strength versus
SPY must be positive, and price must be above VWAP. It uses the **Liquid movers** universe filter.
The setup's one-hour cooldown allows a later qualifying run to alert again while limiting repeats.
It does not specifically detect a pullback/reset; a new pair of events after the cooldown is
required. The alert's evidence shows which two events qualified.

On September 24, WFC had a VWAP support alert at 10:42 a.m. Pacific and a bullish EMA cross at
10:45 a.m. Pacific. Those events illustrate the intended earlier watch point; this new setup was
saved later and did **not** retroactively emit an alert for that sequence. A late ORB alert is
different: it reports a break of the first 15-minute opening range, not the beginning of a
momentum move.

## Tight Range Breakout

**What happened:** price closed outside the high or low of five completed 5-minute candles.
That recent range must be no wider than 0.4 times the stock's daily ATR. The breakout
one-minute bar needs at least 1.2 times the range's average per-minute volume.

**What else must pass:** time-of-day RVOL at least 1.0 and positive 5-minute relative
strength versus SPY. The setup uses the Liquid movers universe filter.

**What it does not establish:** progressively smaller candles, declining volume before
the break, contracting ATR or Bollinger Bands, or a forceful expansion after the break.
This is a narrow-range break, not a full volatility-compression pattern.

## The names used in Config

- **Indicator:** a calculated value such as EMA, VWAP, ATR, or RVOL.
- **Trigger:** an event, such as an EMA cross or a break above a range. The numbers inside a trigger are its settings.
- **Check:** a current-bar requirement, such as RVOL at least 1.0. Checks do not fire by themselves.
- **Universe filter:** which symbols are eligible for the setup during this session.
- **Setup:** the named rule that combines those pieces and controls repeats.
- **Alert:** the message Edge publishes when a setup qualifies. Trader decides separately whether to act.

In **Setup check**, a **Waiting** row means some triggers matched but the setup
still needs more. For example, “2 of 7 triggers matched within 1 min” describes
progress toward one setup alert; it does **not** mean two alerts were sent.

The stable IDs in saved configurations and alert messages keep their existing names for compatibility, even when a display name changes.

## Indicator choices

Config → Setups → Parameters contains Classic-backed SMA, EMA, ATR, ADX and
directional indicators, MACD line/signal/histogram, RSI, Stochastic %K/%D,
CCI, Bollinger bands/%B/bandwidth, and OBV. Checks are *states*: they do not
create an alert by themselves. Six new completed-candle triggers can alert on
a configured level crossing by MACD histogram, RSI, Stochastic %K, CCI,
Bollinger %B, or ADX. Add at least one trigger to a setup and use checks to
restrict it. No existing saved setup was automatically changed. VWAP remains
available through the existing VWAP checks/triggers. Time-of-day RVOL and
relative strength versus SPY/sector are Edge-specific, not Classic indicators.

All Classic values can be unavailable during warm-up; unavailable checks block
the setup, and unavailable triggers do not fire. These are technical conditions,
not trade recommendations. See the [indicator cutover](INDICATOR_MIGRATION.md)
and [trigger contracts](TRIGGER_CONTRACTS.md) for calculation and timing details.

## Saved setup overview

| Setup | Enabled in this snapshot | What it watches |
|---|---|---|
| 5MIN HOD / 5MIN LOD | No | Break of a recent five-minute high or low with strong recent volume. |
| 60-Day High/Low | Yes | First true breach of a 60-session high or low; needs enough daily history. |
| ADX Trend Pullback | Yes | Return to the 5-minute EMA20 in an established, ADX-confirmed trend with volume and sector strength. |
| Tight Range Breakout | Yes | Exit from a narrow recent intraday range on qualifying volume. It does not require progressive compression. |
| Daily ATR Extension | Yes | A new five-minute extreme when already far from the prior daily EMA8; still a tuning draft. |
| Directional High-RVOL ORB | Yes | Five-minute opening-range break agreeing with the opening candle and ranking highly on opening volume; only before 11:00 ET. |
| EMA Cross | Yes | Intraday EMA(3) crossing EMA(9), subject to volume, strength and other gates. |
| Episodic Pivot Structure (Draft) | Yes | Gap, early 15-minute ORB and high RVOL; **does not verify an earnings/news catalyst**. |
| Exit Trade Watch | Yes | Long-position warning when price crosses below the five-minute EMA9; it does not manage an order. |
| Failed Swing Break (Draft) | Yes | Same-candle rejection of a recent swing level; **not** a fully ordered break-then-fail sequence. |
| HOD Breakout | Yes | Strong new high with multiple timeframe breaks, volume spike and green-candle streak. |
| Key Level Cross | Yes | Cross of daily SMA50/SMA200 or prior-day high/low with volume. |
| LOD Breakdown | No | Short-side mirror of HOD Breakout. |
| Momentum Watch | Yes | Two confirming signs of developing upside momentum within 15 minutes; detailed rule above. |
| New HOD / New LOD | Yes | Bare new high or low of the day; these are intentionally broad. |
| ORB | Yes | Break of the first 15-minute opening range, even if that break happens much later. |
| Trend | Yes | Three consecutive five-minute candles in one direction with sustained VWAP/EMA alignment; watchlist-style alert. |
| V off VWAP (support/resistance) | Yes | Sharp approach to VWAP and rejection/bounce rather than a prolonged hover. |
| VWAP Cross | Yes | Price closing through VWAP with its configured gates. |
| VWAP Support/Resistance | Yes | Candle bouncing from VWAP support or rejecting VWAP resistance. |

## How to read and tune an alert

- **Trigger** tells you what just happened; **checks** and **universe filter** explain why this
  symbol qualified. Two alerts with similar chart shapes can have different gates.
- **ORB** means opening-range breakout. The 15-minute ORB is about a *level*, not a deadline.
- **RVOL** compares volume with the expected volume for that time of day. **Relative strength**
  compares the stock's recent move with SPY or, where specified, its sector ETF.
- A completed candle is necessary for most triggers. The alert's market timestamp identifies the
  source bar; publication normally follows when that bar completes.
- Use **Config → Setups → select a setup** for exact live rules and **Setup Check** to inspect why a
  symbol does or does not currently qualify.

For the full configuration workflow, see the [User Guide](../USER_GUIDE.md#5-setups). For every
native trigger's session, boundary and re-arm rule, see [Trigger Contracts](TRIGGER_CONTRACTS.md).
For programs consuming alerts, see [Alert Feed](ALERT_FEED.md).
