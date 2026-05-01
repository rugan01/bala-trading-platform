# Historical LLM Handover Guide

This file is preserved as a deep implementation handoff snapshot from the earlier local workspace.
Some paths, commands, and assumptions are historical.
Use the current README files and `docs/operations/runbook.md` for public-facing instructions.

# LLM Handover Guide

This document is the operational handoff for continuing this project in another LLM such as Claude, Gemini, or another Codex thread.

It explains:
- what the project contains
- what is stable versus fragile
- where live market data is coming from
- what mistakes already happened and how to avoid them
- how to continue extending the analyzers safely

## Project purpose

This project is a local Upstox-powered trading-analysis workspace with four analyzers / monitors:

1. `mcx_monitor`
2. `stock_fo_monitor`
3. `stock_intraday_monitor`
4. `index_expiry_monitor`

It is intentionally an **analysis / screening / monitoring** project, not an execution bot.

## Project layout

- `/Users/rugan/Projects/upstox-analyzer/mcx_monitor`
- `/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor`
- `/Users/rugan/Projects/upstox-analyzer/stock_intraday_monitor`
- `/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor`
- `/Users/rugan/Projects/upstox-analyzer/output`
- `/Users/rugan/Projects/upstox-analyzer/run_all.sh`
- `/Users/rugan/Projects/upstox-analyzer/README.md`

Credentials are read from:
- `/Users/rugan/balas-product-os/.env`

Expected token keys:
- `UPSTOX_ACCESS_TOKEN`
- `ACCESS_TOKEN`
- `UPSTOX_TOKEN`

## What each analyzer does

### 1. MCX monitor

Main file:
- `/Users/rugan/Projects/upstox-analyzer/mcx_monitor/analyze_mcx.py`

Tracks:
- `GOLDM`
- `SILVERMIC`
- `CRUDEOILM`
- `ZINCMINI`
- `NATGASMINI`

Key behavior:
- uses Upstox live quotes
- uses recent `15m` and daily candles
- scores each commodity bullish / bearish / neutral
- creates practical trade plans with:
  - entry
  - stop
  - T1
  - T2
  - trailing level
  - lot-size-aware rupee risk
  - allowed lots
  - `Trade / Reduce size / Skip`
- compares current score vs previous run using `state.json`

Outputs:
- `/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor/latest_report.md`
- `/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor/state.json`

### 2. Stock / index F&O analyzer

Main file:
- `/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/analyze_stock_fo.py`

Universe file:
- `/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt`

Modes:
- `weekly`
- `monthly`

Key behavior:
- daily trend analysis using:
  - MA `8/20/50/100`
  - `Supertrend(10,3)`
  - `RSI(14)`
  - `ADX(14)` with `+DI/-DI`
  - 52-week high / low proximity
  - relative strength vs `Nifty 50` and `Nifty 500`
- option-chain analysis using Upstox live chain
- exact spread selection for stocks
- VIX-aware debit vs credit selection
- volatility-surface summary:
  - ATM IV
  - skew
  - term structure
- spread-level Greeks
- OI-vs-price interpretation
- index-specific structures:
  - current-month iron condors
  - next-month ratio spreads
- staged index campaign builder:
  - next monthly-style expiry leg 1
  - month-after leg 2
  - payoff map
  - add guidance
  - tail-risk note

Outputs:
- `/Users/rugan/Projects/upstox-analyzer/output/stock-fo/weekly_report.md`
- `/Users/rugan/Projects/upstox-analyzer/output/stock-fo/monthly_report.md`

### 3. Stock / index intraday analyzer

Main file:
- `/Users/rugan/Projects/upstox-analyzer/stock_intraday_monitor/analyze_intraday.py`

Key behavior:
- ranks bullish / bearish intraday names using:
  - relative intraday strength vs Nifty
  - daily trend filters
  - narrow CPR
  - daily ADX direction
- includes index regime for:
  - `NIFTY`
  - `BANKNIFTY`
  - `SENSEX`
- includes Tuesday Nifty `0DTE` note
- includes Thursday Sensex `0DTE` note using Nifty as proxy
- includes non-0DTE small-swing index guidance after the first 30 minutes

Output:
- `/Users/rugan/Projects/upstox-analyzer/output/stock-intraday/intraday_report.md`

### 4. Index expiry monitor

Main file:
- `/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor/sensex_expiry_short_straddle.py`

Purpose:
- monitor the Thursday SENSEX expiry short-straddle “seller’s day” setup
- capture the `9:16` baseline spot from Upstox live quote and ATM CE/PE premiums from Upstox live option chain
- evaluate decay versus the `9:16` premium at or after `9:35`
- open a **paper** short straddle if the setup qualifies
- monitor every `5 minutes` for:
  - current spot
  - current combined premium
  - paper P&L
  - spot stop of `±500` points from the `9:16` baseline

Outputs:
- `/Users/rugan/Projects/upstox-analyzer/output/index-expiry/sensex_expiry_report.md`
- `/Users/rugan/Projects/upstox-analyzer/output/index-expiry/sensex_expiry_state.json`

## Important live-data lessons already learned

These are the most important operational lessons from the build.

### 1. Upstox live quote API is the source of truth for intraday spot checks

For live checks on stocks, indices, and options, the most reliable endpoint used in this project has been:
- `https://api.upstox.com/v2/market-quote/quotes`

It returns:
- live `last_price`
- open / high / low / previous close
- market depth
- volume
- average price
- and for options, often the current best executable bid/ask context

When checking live levels for a specific open position, use this quote endpoint first.

### 2. Upstox `15m` historical candles can be stale intraday

This was a real issue discovered during live usage.

Observed problem:
- the `stock_intraday_monitor` was producing wrong intraday trade levels for names like `LT`
- the user spotted this because the report levels were far away from the real live price
- root cause: Upstox `15m` historical candles were returning bars from a **prior session**, not the current trading day

What was changed:
- `analyze_intraday.py` now detects whether the latest `15m` candle belongs to **today**
- if not, it marks the setup as non-actionable and suppresses precise intraday levels
- it explicitly tells the user that the intraday candle feed is stale and live chart confirmation is required

This was the correct fix.

Rule for future LLMs:
- do **not** trust `15m`-derived entry / SL / target values unless the latest candle date is confirmed as the current session date
- if the feed is stale, keep the analyzer as ranking / bias only

### 3. Exact contract resolution matters a lot

We had a real mistake here too.

Problem encountered:
- while checking a live `CGPOWER` option spread, the wrong expiry month was used first
- that produced completely wrong option values

Correct approach:
- always resolve the exact instrument from the Upstox instrument master before checking a position
- do **not** assume month / strike / instrument key from memory
- use the official instrument file to match:
  - underlying symbol
  - option type
  - strike
  - exact expiry text

For example, the correct live check for the user's actual spread was:
- `CGPOWER 750 CE 28 APR 26`
- `CGPOWER 800 CE 28 APR 26`

and not the May series.

Rule for future LLMs:
- whenever reviewing a live option position, resolve the exact contract first from the instrument master
- then query quotes using that exact instrument key

### 4. For wide or illiquid option books, manage from the underlying

Another important practical learning:
- some farther-dated or thinner option strikes had wide bid/ask spreads or weak last-trade information
- in those cases, exact option MTM can be noisy

The safest approach used in conversation:
- use the option chain for approximate executable context
- but manage the trade primarily using the **underlying spot levels**

This was especially important for debit spreads.

Example framework used for `CGPOWER`:
- above confirmation zone: hold
- first warning below nearby support
- stronger caution below the next support
- full invalidation below structural support

Rule for future LLMs:
- if the option book is wide, use the underlying for stop / trail / conversion decisions
- use option prices as a secondary read, not the primary one

### 5. SENSEX key usage is different from the naive `BSE_FO|SENSEX` assumption

This came up directly while investigating why SENSEX wasn’t working.

Correct usage:
- `BSE_INDEX|SENSEX`
  - spot quote
  - historical candles
  - option contract lookup
  - option chain underlying
- `BSE_FO|<numeric_token>`
  - individual option contracts only

Incorrect:
- `BSE_FO|SENSEX` for quote/historical calls

Observed behavior:
- `BSE_INDEX|SENSEX` worked for:
  - quotes
  - LTP
  - historical candles
  - option contract lookup
  - option chain
- `BSE_FO|SENSEX` returned invalid-instrument style failures for historical access

Rule for future LLMs:
- when working with SENSEX on Upstox, treat the index underlying and option contracts separately
- do not assume the NSE-style shorthand carries over

### 6. Account-specific token freshness matters

Observed on April 16, 2026:
- `UPSTOX_ACCESS_TOKEN` / `BALA` was live during the first setup pass
- `UPSTOX_NIMMY_ACCESS_TOKEN` was initially not live
- after the user refreshed `NIMMY`, live quote access for `BSE_INDEX|SENSEX` succeeded

Implication:
- data/monitor scripts that select an account must validate connectivity on startup
- if the selected account token is invalid, fail clearly and mention the refresh timestamp

The SENSEX expiry monitor now does this.

### 7. SENSEX expiry monitor risk behavior

Script:
- `/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor/sensex_expiry_short_straddle.py`

Current behavior:
- paper-first; no live orders are placed
- captures the `09:16` baseline spot and ATM straddle premium from live quote / live option-chain snapshots only
- tracks the 09:16 reference short-straddle decay separately
- at/after `09:35`, opens a monitored paper short straddle only if the 09:16 premium was greater than `0.5%` of spot and current premium has decayed
- uses the `09:16` ATM strike and `09:16 spot ± 500` as the underlying stop band
- exits on spot stop, force-exit time, or configured rupee max-loss
- default max loss is `₹2000` via `--max-loss-rupees 2000`
- default clean-entry baseline lag limit is `60` seconds via `--max-baseline-lag-seconds 60`
- active-risk mode polls open positions every `5` seconds via `--active-risk-poll-seconds 5`, even if the normal `--poll-seconds` value is slower
- report/state include an `Active Risk Monitor` with `MONITOR / CAUTION / DANGER / EXIT_READY / EXIT_NOW`
- Telegram alerts use `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from `/Users/rugan/balas-product-os/.env`
- default risk alerts fire at loss `₹1000/₹1500/₹1800/₹2000`, profit `₹500/₹1000/₹1500/₹2000`, and spot distance `100/50/25/0` points from either stop

Live-data rule:
- do not use the historical candle API for the SENSEX expiry monitor's 09:16 baseline, entry decision, stop decision, or P&L
- historical candles may be used only for previous-day CPR/pivot context
- if the monitor starts late, it can show a late live snapshot for paper observation, but it must not enter unless the baseline lag is within the configured limit
- if the user has manually captured the live 09:16 baseline, use `--manual-baseline-spot`, `--manual-baseline-strike`, and `--manual-baseline-combined`; this sets only the baseline, while all monitoring still uses Upstox live quote/option-chain data

Actual live position monitor:
- script: `/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor/sensex_live_position_monitor.py`
- use when the user has already entered a live SENSEX options position in NIMMY and wants monitoring only
- reads Upstox short-term positions, filters open `BFO` `SENSEX` legs with non-zero quantity, and computes net live P&L
- default monitoring command uses `--max-loss-rupees 2000 --profit-target-rupees 5000 --poll-seconds 5`
- sends Telegram alerts for `EXIT_NOW` at max loss and `BOOK_PROFIT_EXIT_NOW` at profit target
- sends a Telegram heartbeat every 15 minutes by default via `--heartbeat-minutes 15`; heartbeat includes spot, net P&L, action, room to max loss/target, and leg-level LTP/P&L
- stops automatically at market close via `--market-close-time 15:30` and writes `Lifecycle: STOPPED`
- does not place orders

Important safety note:
- live order placement is intentionally not enabled yet
- before enabling it, add explicit live confirmation flags, two-leg order failure handling, state persistence for order IDs, and tested close-order logic

## Specific learning from the live `CGPOWER` trade

The user entered:
- long `CGPOWER 750 CE 28 APR 26` at `19.5`
- short `CGPOWER 800 CE 28 APR 26` at `5.4`
- net debit `14.1`

Correct live validation was done using the exact April expiry.

Underlying context used for trade management:
- above `742`: constructive / hold
- `736-742`: caution zone
- below `736`: consider reduce / exit
- below `727`: bullish structure broken

Key lesson:
- the initial wrong answer happened because the May expiry was checked by mistake
- this should not be repeated in future handoffs

## How another LLM should work on this project

If another LLM picks this up, it should follow this order:

1. Read:
   - `/Users/rugan/Projects/upstox-analyzer/README.md`
   - this file: `/Users/rugan/Projects/upstox-analyzer/LLM_HANDOVER.md`

2. Identify which analyzer is relevant:
- MCX intraday futures -> `mcx_monitor`
- weekly / monthly stocks and indices -> `stock_fo_monitor`
- daily intraday stocks and indices -> `stock_intraday_monitor`

3. Before trusting live intraday levels, verify the data source:
- live quote endpoint is okay
- `15m` history may be stale

4. Before analyzing an option position:
- resolve exact strike and expiry from Upstox instrument master
- only then check the quote endpoint

5. For position management:
- prefer underlying-based management when option market depth is wide or patchy

## What is stable versus what is fragile

### Stable
- project layout
- README instructions
- MCX analyzer logic
- stock swing / positional analyzer logic
- option chain integration
- VIX / IV / skew / term / Greeks reporting
- OI-vs-price classification
- index campaign builder
- run-all workflow

### Fragile / watch closely
- Upstox token expiry
- live API response shapes
- intraday `15m` historical freshness
- option-chain liquidity on farther strikes
- exact instrument resolution if expiry naming changes

## Recommended handoff prompt for another LLM

If you want to continue this project in Claude or Gemini, give it something like this:

> This project lives at `/Users/rugan/Projects/upstox-analyzer`. Read `/Users/rugan/Projects/upstox-analyzer/README.md` and `/Users/rugan/Projects/upstox-analyzer/LLM_HANDOVER.md` first. It contains three analyzers: MCX intraday futures, stock/index F&O weekly-monthly analyzer, and stock/index intraday analyzer. Upstox live quotes are the reliable live source. Be careful: the Upstox `15m` historical endpoint can return stale prior-session candles intraday, so do not trust exact intraday entry/SL/target levels unless the latest candle date is today. For live option-position checks, always resolve the exact strike and expiry from the Upstox instrument master before checking quotes. Manage wide or illiquid option spreads primarily from the underlying spot levels. Continue from the current project state without moving files or changing the output structure unless necessary.

## Recommended daily usage

### To run everything

```bash
zsh "/Users/rugan/Projects/upstox-analyzer/run_all.sh"
```

### To run only MCX

```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/mcx_monitor/analyze_mcx.py"   --env-file "/Users/rugan/balas-product-os/.env"   --output-dir "/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor"   --max-risk-rupees 2000
```

### To run stock weekly / monthly

```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/analyze_stock_fo.py"   --env-file "/Users/rugan/balas-product-os/.env"   --universe-file "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt"   --output-dir "/Users/rugan/Projects/upstox-analyzer/output/stock-fo"   --mode weekly   --top 8
```

```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/analyze_stock_fo.py"   --env-file "/Users/rugan/balas-product-os/.env"   --universe-file "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt"   --output-dir "/Users/rugan/Projects/upstox-analyzer/output/stock-fo"   --mode monthly   --top 8
```

### To run stock intraday

```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/stock_intraday_monitor/analyze_intraday.py"   --env-file "/Users/rugan/balas-product-os/.env"   --universe-file "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt"   --output-dir "/Users/rugan/Projects/upstox-analyzer/output/stock-intraday"   --top 8
```

## Final note

The most important handoff lesson is this:

- use live quotes for real-time checks
- treat `15m` historical data cautiously
- resolve exact option contracts before quoting them
- manage option spreads from the underlying when the book is wide

That will prevent almost all of the live-analysis mistakes that happened during development.
