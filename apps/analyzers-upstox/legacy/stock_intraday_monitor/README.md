# Stock Intraday Analyzer

This bundle is the intraday companion to the swing / weekly stock F&O analyzer.

It is designed to answer:

- which Nifty F&O stocks are strongest and weakest **today**
- which names are showing the best **relative intraday performance vs Nifty**
- whether a **narrow CPR** is in place and likely to support expansion
- what the **entry / SL / T1 / T2 / trailing level** looks like using `15m` structure
- how to read the **index backdrop** for:
  - Tuesday `NIFTY` 0DTE
  - Thursday `SENSEX` 0DTE, using Nifty as the main proxy
  - normal non-0DTE index intraday trades after the first 30 minutes

## Files

- `analyze_intraday.py`
  - reads the Upstox token from your `.env`
  - reads the stock universe from the existing F&O watchlist
  - fetches live quotes
  - fetches daily and `15m` candles
  - computes:
    - daily `MA20 / MA50 / MA100`
    - daily `RSI(14)`
    - daily `ADX(14)`, `+DI`, `-DI`
    - `Supertrend(5,3)` and `Supertrend(5,1.5)` on `15m`
    - previous-day CPR and CPR width
    - relative intraday strength vs Nifty
  - ranks the strongest bullish and bearish names
  - creates a compact best-longs / best-shorts shortlist
  - builds a daily index intraday plan with:
    - confidence
    - trigger after 30 minutes
    - invalidation
    - management
    - option-plan note
  - creates commodity-style trade plans:
    - entry trigger
    - stop loss
    - target 1
    - target 2
    - trailing level

## Main factors in the score

- live price vs previous close
- live price vs open
- live price vs average traded price
- live location within the current day range
- relative intraday performance vs Nifty
- daily trend alignment
- `15m` trend alignment
- narrow CPR bonus
- daily ADX direction

## Narrow CPR logic

The script computes previous-day CPR width as:

- `abs(TC - BC) / previous_close * 100`

and tags it as:

- `narrow`
- `normal`

Narrow CPR is treated as a positive filter when we want names capable of better day expansion.

## Trade-plan logic

For bullish setups:

- entry above recent `15m` swing high
- stop from the tighter valid level among:
  - micro `15m` swing low
  - `ST(5,3)`
- `T1` from `1:1` or the nearest practical key level
- `T2` from the next key level or `2R`
- trail with `ST(5,1.5)`

For bearish setups the logic is mirrored.

## Index handling

The report always includes:

- `NIFTY`
- `BANKNIFTY`
- `SENSEX`

Special handling:

- Tuesday:
  - `NIFTY` is the main `0DTE` index focus
- Thursday:
  - `SENSEX` is monitored, but the analyzer still uses `NIFTY` as the primary proxy because of the strong correlation
- Other days:
  - the analyzer gives a small-swing intraday index plan
  - the idea is to wait for the first 30 minutes and trade only after price stabilizes relative to pivot / CPR / average price / key short-term structure

## Output

Run:

```bash
mkdir -p "/Users/rugan/Projects/upstox-analyzer/output/stock-intraday"

python3.11 "/Users/rugan/Projects/upstox-analyzer/stock_intraday_monitor/analyze_intraday.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --universe-file "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/stock-intraday" \
  --top 8
```

Then read:

- `/Users/rugan/Projects/upstox-analyzer/output/stock-intraday/intraday_report.md`

The report includes:

- a best actionable shortlist
- index intraday regime
- daily index intraday plan
- top bullish intraday candidates
- top bearish intraday candidates

## Notes

- This is an analyzer, not an execution bot.
- It is intentionally tuned for **trend day selection**, not low-volatility mean reversion.
- If the market is already too extended, the report may still show a directional bias but the setup may not be marked as strongly actionable.
