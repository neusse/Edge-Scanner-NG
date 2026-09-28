# Standalone Alpaca stock-stream probe

This is a bounded diagnostic, **not** a second Edge data provider. It does not start
or reconfigure the scanner, connect to Schwab, place orders, or save market data.
It uses `alpaca_paper_api_key` and `alpaca_paper_secret` from the environment and
never falls back to live-trading credentials.

From the repository root on Windows:

```powershell
.\.venv\Scripts\python.exe scripts\probe_alpaca_stream.py SPY AAPL WDC --feed iex --duration 65
```

The default requests quotes, trades, and 1-minute bars. Change the symbols to
test other stocks (maximum 30 per invocation), or use `--services` with any of
`quotes,trades,bars,updated_bars,daily_bars,statuses`. `--duration` is 5–120
seconds. The process stops its WebSocket when the time expires; it is not a
background service. Run `--help` for all options.

Read `accepted` to see which symbol/channel subscriptions the server acknowledged.
Read `counts` to see which channels actually sent events during the test. Zero
events in a short window, especially for minute bars or quiet symbols, is **not**
proof that the subscription failed. `lag_ms` compares receipt with the market
timestamp; for minute bars it uses the end of the minute. This depends on the
computer's clock being accurate; small negative lags can be clock skew.
`latest` shows the last bid/ask and sizes, trade price and size, and OHLCV bar
received for each symbol. `samples` keeps the first two events per symbol and
channel, including their prices. An IEX bar's volume is IEX-only volume.

## Result on September 25, 2026

- The paper account accepted IEX quotes, trades, and minute bars for SPY, AAPL,
  and WDC. A 65-second run received all three event types for all three symbols.
  Event timestamps were within roughly one second of receipt, **not 15 minutes
  delayed**. The observed negative 0.1–0.3 second lag suggests local clock skew;
  this test does not certify sub-second network latency.
- It also acknowledged `updatedBars`, `dailyBars`, and `statuses` for those three
  symbols. An eight-second window did not include an event in those channels.
- A separate 30-symbol minute-bar subscription using the first 30 symbols in
  `data/universe.csv` was acknowledged, including SPCX. This confirms those
  subscriptions, not that each symbol produces useful IEX activity or bars.
- At 11:22 Pacific, the IEX WDC 1-minute bar was O 456.44, H 456.44,
  L 456.275, C 456.275, V 747. The same closed minute from the running
  Schwab-backed Edge chart endpoint was O 456.315, H 456.6282, L 456.16,
  C 456.275, V 3,895. The close matched, but IEX captured only about 19% of
  Schwab's volume and a narrower high/low range. This is one illustrative
  minute, not a measured average coverage ratio.
- The account rejected the SIP stream with `insufficient subscription`.
  The installed `alpaca-py` stock-stream client accepts only IEX and SIP feeds;
  `delayed_sip` is not a usable WebSocket option here.

Alpaca's [market-data plan](https://docs.alpaca.markets/us/v1.1/docs/about-market-data-api)
describes Basic's live stock WebSocket as IEX with a 30-symbol limit. IEX is a
single exchange, so its bars and volume are **not equivalent** to consolidated
Schwab/SIP data. Alpaca's [feed description](https://docs.alpaca.markets/us/docs/real-time-stock-pricing-data)
distinguishes IEX, SIP, and delayed SIP; the latter's availability elsewhere
does not make it available in this streaming client.

No symbol-routing change should be made from subscription acceptance alone.
Before assigning lower-priority symbols to IEX, compare several market sessions
for bar coverage, missing minutes, volumes, and alert behavior against Schwab.
