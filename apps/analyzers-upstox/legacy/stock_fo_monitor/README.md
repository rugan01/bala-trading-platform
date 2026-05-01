# Stock F&O Analyzer

This bundle analyzes Nifty / liquid F&O stocks plus the major market indices to help with:

- weekly option-position selection for the coming week
- positional selection near the end of the month for next-month trades

## Files

- `analyze_stock_fo.py`
  - reads the Upstox token from your `.env`
  - reads the stock universe from `universe_nifty_fo.txt`
  - fetches live quotes where available
  - fetches daily candles for all stocks and benchmark indices
  - computes:
    - `MA 8 / 20 / 50 / 100`
    - default `Supertrend(10,3)`
    - `RSI(14)`
    - `ADX(14)`, `+DI`, `-DI`
    - 52-week high / low proximity
    - relative strength versus:
      - `Nifty 50`
      - `Nifty 500`
  - scores each stock as:
    - bullish
    - bearish
    - neutral
  - suggests an options structure:
    - bull call debit spread
    - bull put credit spread
    - bear put debit spread
    - bear call credit spread
  - fetches the live option chain for the chosen expiry
  - selects exact strikes using:
    - liquidity
    - risk/reward
    - approximate probability of profit
    - current VIX regime
  - summarizes the volatility surface using:
    - ATM IV
    - put/call skew
    - front-vs-next expiry term structure
  - adds a plain-English volatility interpretation section to the report
  - reports spread-level Greeks:
    - delta
    - theta
    - vega
    - gamma
  - analyzes futures open interest versus price change and classifies it as:
    - long buildup
    - short buildup
    - short covering
    - long unwinding
  - builds separate index-option structures for:
    - current-month iron condors
    - next-month ratio spreads
  - builds a separate staged index campaign section with:
    - leg 1 in the next monthly-style index expiry
    - leg 2 in the monthly-style expiry after that
    - quick payoff map for sideways / slow-trend / fast-breakout paths
    - add-leg-2 timing guidance
    - combined behavior and tail-risk notes
  - writes a markdown report

- `universe_nifty_fo.txt`
  - editable list of symbols to scan

## Weekly vs Monthly mode

### Weekly mode

Use this near the weekend to decide what to trade in the coming week.

Command:

```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/analyze_stock_fo.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --universe-file "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/stock-fo" \
  --mode weekly
```

### Monthly mode

Use this in the last week of the month to decide which names to trade with next-month options.

Command:

```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/analyze_stock_fo.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --universe-file "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/stock-fo" \
  --mode monthly
```

## How the scoring works

The script looks at:

- MA stack:
  - bullish example: `price > 8 > 20 > 50 > 100`
  - bearish example: `price < 8 < 20 < 50 < 100`
- `Supertrend(10,3)`
- `RSI(14)`
- `ADX(14)` with `+DI` and `-DI`
- distance to 52-week high / low
- relative strength against:
  - `Nifty 50`
  - `Nifty 500`

## Spread-selection logic

### Bullish names

- stronger clean trend:
  - `bull call debit spread`
- positive trend but already near resistance:
  - `bull put credit spread`

### Bearish names

- stronger downside trend:
  - `bear put debit spread`
- weak trend or already near support:
  - `bear call credit spread`

## VIX logic

The analyzer reads `India VIX` and classifies the environment as:

- `low`
- `normal`
- `high`

Rule of thumb in the script:

- high VIX:
  - prefer credit spreads
- low / normal VIX:
  - prefer debit spreads when trend quality is strong

This is only a preference, not a hard rule. The final recommendation still depends on:

- option-chain liquidity
- spread width
- approximate POP
- risk/reward

## Index option logic

For `NIFTY` and `BANKNIFTY`, the analyzer now creates a separate section with:

- current-month iron condors
- next-month ratio spreads

This matches your style more closely:

- iron condors in a high-VIX, mixed-trend market
- ratio spreads when you want a small net credit and better payoff if the market drifts in your chosen direction

For index ratio spreads, the script now prefers:

- strikes that are multiples of `500`
- at least `500` points between the bought and sold strikes
- expiries with roughly `2` months left when available

The analyzer also reports spread-level Greeks so you can see whether the trade is:

- theta positive
- vega short
- too directional

## Debit-to-butterfly workflow

If a debit spread moves well in your favor:

- usually after `60%` to `70%` of max value is achieved
- and the move starts slowing near the next major level

you can consider converting it into a butterfly by selling one more farther OTM option.

Examples:

- bullish:
  - long call spread -> call butterfly
- bearish:
  - long put spread -> put butterfly

## Report output

The script writes:

- `/Users/rugan/Projects/upstox-analyzer/output/stock-fo/weekly_report.md`
- or
- `/Users/rugan/Projects/upstox-analyzer/output/stock-fo/monthly_report.md`

The report contains:

- index regime
- India VIX regime
- index-option structures
- index campaign builder
- top bullish candidates
- top bearish candidates
- spread suggestion for each selected stock
- exact strikes and expiry
- approximate POP / RR / max profit / max loss
- volatility-surface notes
- spread-level Greeks
- stop / adjustment trigger notes
- rupee metrics using actual lot size where bounded risk exists
- OI-vs-price interpretation
- plain-English volatility interpretation
- OI-vs-price interpretation
- staged guidance for adding a second index position later rather than forcing both legs at once

## Notes

- The stock universe file is intentionally editable. You can add or remove symbols any time.
- This is an analyzer, not an execution bot.
