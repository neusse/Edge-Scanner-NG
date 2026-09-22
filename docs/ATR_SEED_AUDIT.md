# Wilder ATR seed impact, 2026-09-22

The scanner previously used a recursive first-TR seed for `wilder_atr`. It now
uses the arithmetic mean of the first `length` true ranges and Wilder's
recurrence thereafter. An incomplete OHLC candle resets ATR warm-up.

To quantify the compatibility impact, run the read-only threshold replay:

```powershell
python -m scripts.audit_atr_seed_impact --data-dir data
```

Using the 256 symbols in `data/universe.csv`, the cached Schwab daily history,
and the last 20 available sessions of cached 5-minute bars on 2026-09-22:

| Measure | Result |
| --- | ---: |
| Daily symbol-days compared | 5,120 |
| Completed regular-session 5-minute bars compared | 306,774 |
| ATR(5)% decisions changed at 1%, 2%, or 4% | 0 at each threshold |
| Daily-close 2× ATR(14) extension decisions changed | 1 |
| Intraday 2× ATR(14) extension decisions changed | 6 |
| Changed extension decisions also on a new-high/low candidate bar | 4 |
| Maximum latest ATR(5) absolute change | $0.000123 |
| Maximum latest ATR(14) absolute change | $0.229430 |

All intraday threshold changes belonged to `SKHY`, which had relatively short
cached daily history. The replay is a parameter and trigger-candidate audit; it
does not reproduce one-minute trigger ordering, universe membership, cooldowns,
or emitted alert counts. A full alert replay requires the separate replay mode
tracked in issue #16. The expected compatibility risk is concentrated in newly
added symbols with short daily history or thresholds close to a boundary.
