# Index Expiry Monitor

Index expiry-day monitoring tools, currently centered on the SENSEX sellers-day short-straddle workflow and live position tracking.

## Scripts

- `sensex_expiry_short_straddle.py`
- `sensex_live_position_monitor.py`

## Analysis-only run

```bash
python3.11 apps/analyzers-upstox/legacy/index_expiry_monitor/sensex_expiry_short_straddle.py --analysis-only
```

## Live paper monitor

```bash
python3.11 apps/analyzers-upstox/legacy/index_expiry_monitor/sensex_expiry_short_straddle.py \
  --account NIMMY \
  --lots 1 \
  --max-loss-rupees 2000
```

## Live position monitor

```bash
python3.11 apps/analyzers-upstox/legacy/index_expiry_monitor/sensex_live_position_monitor.py \
  --account NIMMY \
  --max-loss-rupees 2000 \
  --profit-target-rupees 5000
```

## Output

- `data/legacy-analyzers/index-expiry/`

## Important note

These scripts read the repo-local `.env` by default. Telegram alerts are optional and depend on `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

