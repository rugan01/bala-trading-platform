# Session Notes

This file is the lightweight running journal for new observations, live-market learnings, data quirks, and practical trade-management notes.

Use it for:
- new Upstox API quirks
- live-market lessons that should not yet be baked into core analyzer logic
- trade-management examples
- known issues observed during the day
- ideas to revisit later

Keep the long-term architecture in:
- `/Users/rugan/Projects/upstox-analyzer/README.md`
- `/Users/rugan/Projects/upstox-analyzer/LLM_HANDOVER.md`

Use this file for incremental operational notes.

---

## 2026-04-15

### Live data / API learnings

- Upstox live quote endpoint is reliable enough for spot checks:
  - `https://api.upstox.com/v2/market-quote/quotes`
- Upstox `15m` historical candles for stocks can be stale intraday.
- Intraday analyzer was updated so stale `15m` data does not produce false entry / SL / target values.
- For live position checks, always resolve exact option contracts from the Upstox instrument master before quoting prices.

### CGPOWER spread example

User trade:
- Buy `CGPOWER 750 CE 28 APR 26` at `19.5`
- Sell `CGPOWER 800 CE 28 APR 26` at `5.4`
- Net debit `14.1`

Correct live validation used exact April expiry, not May.

Underlying management levels used:
- above `742`: constructive / hold
- `736-742`: caution zone
- below `736`: reduce / reassess
- below `727`: bullish structure broken

Practical lesson:
- manage this style of debit spread primarily from the underlying when the option market is wide or noisy

### Full-universe intraday relative-strength read

Observed strong names during the session included:
- `PFC`
- `CGPOWER`
- `JSWENERGY`
- `BSE`
- `TATAPOWER`

Observed weaker names included:
- `PIDILITIND`
- `SRF`
- `OIL`
- `COALINDIA`
- `JINDALSTEL`

This kind of universe scan is useful even when exact intraday structure from Upstox candles is not fully trustworthy.

### Next things to watch

- Whether Upstox fixes same-day `15m` stock candle freshness consistently
- Whether stock intraday analyzer should switch more of its early-session logic to live quote + opening-range methods
- Whether a separate full-universe relative-strength report should be added as a first-class output

## 2026-04-16

### SENSEX API learnings

- For SENSEX on Upstox, the correct underlying key is:
  - `BSE_INDEX|SENSEX`
- This key works for:
  - live quote
  - LTP
  - historical candles
  - option contract lookup
  - option chain

- `BSE_FO|SENSEX` should **not** be used as a direct quote/historical key.
- Individual SENSEX options use specific contract keys like:
  - `BSE_FO|869554`
  - `BSE_FO|871204`

### SENSEX expiry monitor added

New module:
- `/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor/sensex_expiry_short_straddle.py`

Purpose:
- capture `9:16` SENSEX baseline spot from Upstox live quote and ATM CE/PE premiums from Upstox live option chain
- evaluate seller’s-day premium decay at or after `9:35`
- paper-monitor a short ATM straddle with:
  - `±500` point spot stop from the `9:16` baseline
  - configurable rupee max-loss circuit breaker
  - `5-second` active-risk polling once a position is open
- track the 9:16 reference short-straddle decay separately so the 9:35 seller's-day confirmation is auditable
- send Telegram alerts for loss thresholds, profit thresholds, and spot-distance-to-stop thresholds when Telegram env keys are present

### Actual SENSEX live-position monitor added

New module:
- `/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor/sensex_live_position_monitor.py`

Purpose:
- monitor already-entered live SENSEX option positions in the NIMMY account
- read open `BFO` `SENSEX` legs from Upstox short-term positions
- compute net live P&L every few seconds
- alert at `-₹2000` max loss or `₹5000` profit target
- send Telegram heartbeat every `15` minutes with live status and leg P&L
- stop automatically at `15:30` market close
- never place live exit orders

Correction:
- the live monitor must not use historical candles for the 09:16 baseline, entry decision, stop decision, or P&L
- historical candles are acceptable only for previous-day CPR/pivot context
- a late live baseline may be displayed for paper observation, but entry is blocked unless the baseline lag is within the configured limit

### Account-token note

Observed:
- `BALA` token was live during the first setup pass
- `NIMMY` token was initially stale, then refreshed by the user
- after refresh, `NIMMY` live quote access for `BSE_INDEX|SENSEX` succeeded

Practical lesson:
- any account-selectable monitor should validate the selected account token on startup
- if invalid, fail with a useful message instead of pretending SENSEX data itself is unavailable
