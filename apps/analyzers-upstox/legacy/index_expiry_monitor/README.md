# Index Expiry Monitor

This folder contains index expiry-day intraday monitors and strategy scripts.

Current script:
- [sensex_expiry_short_straddle.py](/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor/sensex_expiry_short_straddle.py)

## SENSEX Sellers-Day Short Straddle

This is a **paper-first monitor** for the Thursday SENSEX expiry idea:

1. Use Upstox **live quote** for the SENSEX spot baseline and ongoing spot checks
2. Use Upstox **live option chain** for baseline CE/PE premium and ongoing premium checks
3. Capture the `9:16` baseline spot and ATM straddle premium from live snapshots only
4. Check whether:
   - `9:16 ATM straddle premium > 0.5% of 9:16 spot`
5. At or after `9:35`, if the current premium has decayed versus the `9:16` premium:
   - open a **paper short ATM straddle** using the `9:16` ATM strike
6. Monitor every `5 minutes`:
   - current spot
   - current combined premium
   - 9:16 reference short-straddle decay/P&L
   - paper P&L
7. Exit if:
   - spot rises `500` points above the `9:16` spot
   - spot falls `500` points below the `9:16` spot
   - configured rupee max-loss is hit
   - or configured EOD force-exit time is hit

## Important key clarification

Upstox key usage for SENSEX is:

- **spot live quote / option chain underlying**
  - `BSE_INDEX|SENSEX`
- **individual option contract keys**
  - actual keys like `BSE_FO|869554`

Do **not** use:
- `BSE_FO|SENSEX`

for live quotes or option-chain access. That is not a valid direct quote instrument key.

Live strategy rule:
- Do not use the historical candle API for the 09:16 baseline, live premium checks, entry decisions, stop checks, or P&L.
- Historical data may be used only for previous-day context such as CPR/pivots.

## Commands

### Analysis only
```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor/sensex_expiry_short_straddle.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/index-expiry" \
  --account BALA \
  --analysis-only
```

### Live paper monitor
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

For today’s 1-lot validation, start this before `09:16`.

The monitor will:
- capture the `09:16` SENSEX spot and ATM straddle premium from live quote / live option-chain snapshots
- keep tracking the 09:16 reference short-straddle decay
- only open the monitored paper short straddle at/after `09:35` if premium has decayed
- use the `09:16` ATM strike and `09:16` spot-based stop band
- exit the monitor if SENSEX moves beyond `09:16 spot ± 500`, if paper loss reaches `₹2000`, or at the force-exit time
- poll every `5` seconds before entry so the 09:16 baseline and 09:35 check are not missed
- after entry, force active-risk polling every `5` seconds even if `--poll-seconds` is higher
- write a loud `Active Risk Monitor` section with `MONITOR / CAUTION / DANGER / EXIT_READY / EXIT_NOW`
- send Telegram alerts when configured loss, profit, or spot-distance thresholds are crossed

If the monitor is not running before 09:16, it can still take a late live snapshot inside the configured grace window for paper validation, but it will not enter unless the baseline lag is within `--max-baseline-lag-seconds`.

### Manual 09:16 baseline override

If the 09:16 spot/strike/premium was captured manually from the live chart, pass it explicitly:

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
  --manual-baseline-spot 78553 \
  --manual-baseline-strike 78500 \
  --manual-baseline-combined 421 \
  --manual-baseline-time 09:16
```

The override only sets the baseline. Current spot, current CE/PE premium, entry, P&L, and exits still use Upstox live quote and live option-chain data.

### Active risk alerts

Default alert thresholds:
- loss alerts at `₹1000`, `₹1500`, `₹1800`, `₹2000`
- profit alerts at `₹500`, `₹1000`, `₹1500`, `₹2000`
- stop-distance alerts when spot is within `100`, `50`, `25`, or `0` points of either stop

Telegram alerts use:
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

from `/Users/rugan/balas-product-os/.env`. Use `--disable-telegram-alerts` if you only want report/state updates.

## Outputs

The script writes:
- `/Users/rugan/Projects/upstox-analyzer/output/index-expiry/sensex_expiry_report.md`
- `/Users/rugan/Projects/upstox-analyzer/output/index-expiry/sensex_expiry_state.json`

## Actual Live Position Monitor

Use this when a live SENSEX options position already exists in the NIMMY account and we only need monitoring/alerts, not order placement.

```bash
python3.11 "/Users/rugan/Projects/upstox-analyzer/index_expiry_monitor/sensex_live_position_monitor.py" \
  --env-file "/Users/rugan/balas-product-os/.env" \
  --output-dir "/Users/rugan/Projects/upstox-analyzer/output/index-expiry" \
  --account NIMMY \
  --max-loss-rupees 2000 \
  --profit-target-rupees 5000 \
  --poll-seconds 5 \
  --heartbeat-minutes 15 \
  --market-close-time 15:30
```

This monitor:
- reads actual open `BFO` `SENSEX` positions from Upstox short-term positions
- ignores closed SENSEX positions where quantity is `0`
- computes net live P&L across the open legs
- sends `EXIT_NOW` Telegram alerts if net P&L breaches `-₹2000`
- sends `BOOK_PROFIT_EXIT_NOW` Telegram alerts if net P&L reaches `₹5000`
- sends a Telegram heartbeat every `15` minutes with spot, net P&L, action, room to stop/target, and leg-level LTP/P&L
- stops automatically at `15:30` and writes `Lifecycle: STOPPED`
- writes `/Users/rugan/Projects/upstox-analyzer/output/index-expiry/sensex_live_position_report.md`
- writes `/Users/rugan/Projects/upstox-analyzer/output/index-expiry/sensex_live_position_state.json`

It does **not** place exit orders. It only tells us when to exit/book profit.

## Token notes

The script validates the selected account token on startup.

If the selected account token is stale:
- refresh the token in `/Users/rugan/balas-product-os/.env`
- rerun the command
- the report will clearly show the connection failure instead of treating it as a SENSEX data issue

As of the latest setup pass on April 16, 2026:
- `NIMMY` is the default account for this monitor
- the monitor is still paper-first and does not place live orders
