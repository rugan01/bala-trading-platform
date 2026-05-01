# Trading Analyzers Reference

## 0. Start Here

If you are continuing this project in another LLM or handing it to someone else, read this first:

- `/Users/rugan/Projects/upstox-analyzer/LLM_HANDOVER.md`

That file explains the architecture, live-data caveats, exact-contract resolution workflow, and the mistakes already discovered so they are not repeated.

---

## 0A. Running Notes

For day-to-day learnings, API quirks, and live trading observations, use:

- `/Users/rugan/Projects/upstox-analyzer/SESSION_NOTES.md`

This keeps the main docs stable while still preserving new lessons.

---

## 0B. Copy-Paste Prompt

If you want to hand this workspace to Claude, Gemini, or another LLM quickly, use:

- `/Users/rugan/Projects/upstox-analyzer/HANDOFF_PROMPT.txt`

This is the shortest copy-paste starting point.

---


This workspace now contains **4 practical analyzers / monitors** built around your Upstox setup:

1. **MCX intraday futures monitor**
2. **Stock / index F&O swing and positional analyzer**
3. **Stock / index intraday analyzer**
4. **Index expiry-day short-straddle monitor**

All of them read credentials from:

- `/Users/rugan/balas-product-os/.env`

The expected token keys are:

- `UPSTOX_ACCESS_TOKEN`
- or `ACCESS_TOKEN`
- or `UPSTOX_TOKEN`

---

## 1. What We Built

### A. MCX monitor

Folder:

- `/Users/rugan/Projects/upstox-analyzer/mcx_monitor`

Main file:

- `/Users/rugan/Projects/upstox-analyzer/mcx_monitor/analyze_mcx.py`

Purpose:

- monitors `GOLDM`, `SILVERMIC`, `CRUDEOILM`, `ZINCMINI`, `NATGASMINI`
- uses live Upstox quotes
- uses recent `15m` and daily candles
- scores each commodity as bullish / bearish / neutral
- creates a trade plan with:
  - entry trigger
  - stop loss
  - target 1
  - target 2 / next level
  - fast supertrend trail
  - lot-size-based rupee risk
  - `Trade / Reduce size / Skip`
- compares current run against previous run to determine whether the thesis is strengthening or weakening

Key output:

- `/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor/latest_report.md`
- `/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor/state.json`

Optional automation:

- macOS `launchd` plist included at:
  - `/Users/rugan/Projects/upstox-analyzer/mcx_monitor/com.rugan.mcx-monitor.plist`

---

### B. Stock / index F&O analyzer

Folder:

- `/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor`

Main files:

- `/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/analyze_stock_fo.py`
- `/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt`

Purpose:

- analyzes Nifty / liquid F&O stocks plus:
  - `NIFTY`
  - `BANKNIFTY`
  - `SENSEX`
- supports:
  - **weekly mode**
  - **monthly mode**
- computes:
  - `MA 8 / 20 / 50 / 100`
  - `Supertrend(10,3)`
  - `RSI(14)`
  - `ADX(14)`, `+DI`, `-DI`
  - 52-week high / low proximity
  - relative strength vs `Nifty 50` and `Nifty 500`
- reads live option chains
- suggests exact spreads and strikes
- includes:
  - probability of profit approximation
  - risk/reward
  - max profit / max loss
  - Greeks
  - volatility surface
  - skew
  - term structure
  - VIX-aware spread choice
  - OI-vs-price interpretation

Also includes:

- **Index Option Structures**
  - current-month iron condors
  - next-month ratio spreads
- **Index Campaign Builder**
  - next monthly expiry leg
  - month-after leg
  - combined outlook
  - payoff map
  - add-leg-2 guidance
  - tail-risk notes

Key outputs:

- `/Users/rugan/Projects/upstox-analyzer/output/stock-fo/weekly_report.md`
- `/Users/rugan/Projects/upstox-analyzer/output/stock-fo/monthly_report.md`

---

### C. Stock / index intraday analyzer

Folder:

- `/Users/rugan/Projects/upstox-analyzer/stock_intraday_monitor`

Main file:

- `/Users/rugan/Projects/upstox-analyzer/stock_intraday_monitor/analyze_intraday.py`

Purpose:

- ranks intraday bullish and bearish Nifty F&O stocks
- uses **relative strength vs Nifty**
- uses **narrow CPR** as a key expansion filter
- builds intraday trade plans from `15m` structure:
  - entry
  - SL
  - T1
  - T2
  - trail

Also includes:

- **Best Actionable Shortlist**
- **Index Intraday Regime**
- **Index Daily Intraday Plan**

Index logic included:

- Tuesday:
  - `NIFTY` as main `0DTE` focus
- Thursday:
  - `SENSEX` `0DTE`, using Nifty as the main proxy
- Other days:
  - small-swing index intraday plan after the first 30 minutes

Key output:

- `/Users/rugan/Projects/upstox-analyzer/output/stock-intraday/intraday_report.md`

---

### D. Index expiry monitor

Folder:

- `/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor`

Main file:

- `/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor/sensex_expiry_short_straddle.py`

Purpose:

- monitors the Thursday `SENSEX` expiry-day short-straddle setup
- uses the correct SENSEX underlying key:
  - `BSE_INDEX|SENSEX`
- uses the `9:16` underlying and option premium as the baseline
- checks whether the ATM straddle premium is greater than `0.5%` of spot
- at or after `9:35`, checks whether the premium has decayed versus the `9:16` baseline
- if yes, opens a **paper short straddle**
- monitors every `5 minutes`:
  - underlying spot
  - current combined premium
  - paper P&L
  - spot-based stop condition

Important instrument-key lesson:

- use `BSE_INDEX|SENSEX` for:
  - live quote
  - historical candles
  - option contract / chain underlying
- use individual contract keys like `BSE_FO|869554` for actual options
- do **not** use `BSE_FO|SENSEX` for quote/historical calls

Key outputs:

- `/Users/rugan/Projects/upstox-analyzer/output/index-expiry/sensex_expiry_report.md`
- `/Users/rugan/Projects/upstox-analyzer/output/index-expiry/sensex_expiry_state.json`

---

## 2. Commands To Run Everything

### A. MCX monitor

```bash
mkdir -p "/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor"

python3.11 "/Users/rugan/Projects/upstox-analyzer/mcx_monitor/analyze_mcx.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor" \
  --max-risk-rupees 2000
```

Read:

- `/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor/latest_report.md`

---

### B. Stock F&O weekly analyzer

```bash
mkdir -p "/Users/rugan/Projects/upstox-analyzer/output/stock-fo"

python3.11 "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/analyze_stock_fo.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --universe-file "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/stock-fo" \
  --mode weekly \
  --top 8
```

Read:

- `/Users/rugan/Projects/upstox-analyzer/output/stock-fo/weekly_report.md`

---

### C. Stock F&O monthly analyzer

```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/analyze_stock_fo.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --universe-file "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/stock-fo" \
  --mode monthly \
  --top 8
```

Read:

- `/Users/rugan/Projects/upstox-analyzer/output/stock-fo/monthly_report.md`

---

### D. Stock / index intraday analyzer

```bash
mkdir -p "/Users/rugan/Projects/upstox-analyzer/output/stock-intraday"

python3.11 "/Users/rugan/Projects/upstox-analyzer/stock_intraday_monitor/analyze_intraday.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --universe-file "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/stock-intraday" \
  --top 8
```

Read:

- `/Users/rugan/Projects/upstox-analyzer/output/stock-intraday/intraday_report.md`

---

### E. SENSEX expiry monitor

```bash
mkdir -p "/Users/rugan/Projects/upstox-analyzer/output/index-expiry"

python3.11 "/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor/sensex_expiry_short_straddle.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/index-expiry" \
  --account BALA \
  --analysis-only
```

Read:

- `/Users/rugan/Projects/upstox-analyzer/output/index-expiry/sensex_expiry_report.md`

For live paper monitoring later:

```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor/sensex_expiry_short_straddle.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/index-expiry" \
  --account NIMMY \
  --lots 1 \
  --max-loss-rupees 2000 \
  --poll-seconds 60 \
  --active-risk-poll-seconds 5 \
  --pre-entry-poll-seconds 5 \
  --baseline-live-capture-grace-minutes 20 \
  --max-baseline-lag-seconds 60
```

This paper monitor captures the `09:16` SENSEX baseline using live quote and live option-chain snapshots only, waits for the `09:35` decay confirmation, then tracks a 1-lot paper short straddle using the baseline ATM strike. It exits on baseline spot `±500`, configured rupee max-loss, or force-exit time. Historical candles are only for previous-day CPR/pivot context, not live baseline or execution decisions. After entry, active-risk mode polls every `5` seconds and sends Telegram alerts from the `.env` Telegram settings when risk thresholds are crossed.

---

## 2A. One-Command Runner

Run everything in one shot:

```bash
zsh "/Users/rugan/Projects/upstox-analyzer/run_all.sh"
```

This will generate:

- MCX report
- weekly stock F&O report
- monthly stock F&O report
- intraday stock/index report

All outputs go under:

- `/Users/rugan/Projects/upstox-analyzer/output`

---

## 3. What Each Report Tells You

### MCX report

- commodity bias
- entry / SL / targets
- trailing level
- lot-size-aware rupee risk
- whether the trade is actually usable under your risk cap
- whether the thesis is strengthening or weakening vs the last run

### Weekly / monthly stock F&O report

- overall market regime
- India VIX regime
- volatility interpretation
- index trend
- index option structures
- index campaign builder
- top bullish and bearish names
- exact spread + expiry
- POP / RR / max profit / max loss
- Greeks
- skew / term structure
- OI-vs-price interpretation

### Intraday stock / index report

- best actionable longs / shorts
- index intraday regime
- index daily intraday plan
- top bullish and bearish intraday names
- narrow CPR filter
- relative strength vs Nifty
- entry / SL / T1 / T2 / trailing level

---

## 4. Where To Edit Things

### Commodity roots

Edit:

- `/Users/rugan/Projects/upstox-analyzer/mcx_monitor/analyze_mcx.py`

### Stock universe

Edit:

- `/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt`

This same universe is also used by the intraday stock analyzer.

### Output locations

Can be changed through the `--output-dir` argument for each script.

### Narrow CPR threshold for intraday stocks

Default:

- `0.40%`

Override:

```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/stock_intraday_monitor/analyze_intraday.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --universe-file "/Users/rugan/Projects/upstox-analyzer/stock_fo_monitor/universe_nifty_fo.txt" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/stock-intraday" \
  --top 8 \
  --narrow-cpr-threshold-pct 0.35
```

---

## 5. Suggested Workflow

### Daily

1. Run the **intraday stock / index analyzer**
2. If trading commodities, run the **MCX monitor**
3. Use the **Index Daily Intraday Plan** before taking any index trade
4. Use the **Best Actionable Shortlist** for stock intraday ideas

### Weekend

1. Run the **weekly stock F&O analyzer**
2. Review:
  - bullish names
  - bearish names
  - VIX regime
  - spread suggestions
  - index campaign builder

### Last week of the month

1. Run the **monthly stock F&O analyzer**
2. Use it to choose:
  - next-month stock spreads
  - staged index positions

---

## 6. Most Important Files To Bookmark

- [Project README](/Users/rugan/Documents/New%20project/README.md)
- [MCX README](/Users/rugan/Documents/New%20project/mcx_monitor/README.md)
- [Stock F&O README](/Users/rugan/Documents/New%20project/stock_fo_monitor/README.md)
- [Stock Intraday README](/Users/rugan/Documents/New%20project/stock_intraday_monitor/README.md)

Reports:

- [MCX latest report](/Users/rugan/Projects/upstox-analyzer/output/mcx-monitor/latest_report.md)
- [Weekly stock F&O report](/Users/rugan/Projects/upstox-analyzer/output/stock-fo/weekly_report.md)
- [Monthly stock F&O report](/Users/rugan/Projects/upstox-analyzer/output/stock-fo/monthly_report.md)
- [Intraday stock report](/Users/rugan/Projects/upstox-analyzer/output/stock-intraday/intraday_report.md)

---

## 7. Important Notes

- These are **analyzers**, not execution bots.
- They depend on:
  - valid Upstox token
  - instrument dumps from Upstox
  - live / historical endpoint availability
- Some after-market snapshots can be less ideal than live market-hour snapshots, but the scripts already handle several of those cases more gracefully now.
