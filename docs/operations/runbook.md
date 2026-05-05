# Runbook

This is the fastest way to run the main tools from the repo root.

## One-time setup

```bash
cd /path/to/bala-trading-platform
cp .env.example .env
PYTHONPATH=packages/trading_platform/src python3.11 -m trading_platform.cli init-db
```

Fill `.env` with your real local values before running anything that depends on Upstox, Notion, or Telegram.

Install journaling dependencies:

```bash
python3.11 -m pip install -r apps/journaling/requirements.txt
```

For `apps/journaling/upstox_token_refresh.py`, use Python `3.12+` because the `upstox-totp`
dependency does not support Python `3.11`. Recommended:

```bash
python3.13 -m venv .venv
./.venv/bin/python -m pip install -r apps/journaling/requirements.txt
```

## Morning brief and learning loop

```bash
cd /path/to/bala-trading-platform

python3.11 apps/briefing/morning_brief.py
python3.11 apps/briefing/live_analysis.py
python3.11 apps/briefing/brief_eod_review.py --source-date YYYY-MM-DD
```

Outputs:
- `data/reports/premarket/`
- `data/archive/platform.sqlite3`

## Trade journaling

Same-day journaling:

```bash
cd /path/to/bala-trading-platform
python3.11 apps/journaling/trade_journaling.py --account BALA
python3.11 apps/journaling/trade_journaling.py --account NIMMY
```

Specific date:

```bash
python3.11 apps/journaling/trade_journaling.py --date YYYY-MM-DD --account BALA
```

Broker XLSX recovery/backfill:

```bash
python3.11 apps/journaling/broker_trade_backfill.py \
  --broker-file /path/to/export.xlsx \
  --date YYYY-MM-DD
```

Tips:
- set `BROKER_UCC_BALA` / `BROKER_UCC_NIMMY` in `.env` if you want broker files to auto-detect the account without `--account`
- the broker backfill path keeps the broker-file trade times and uses Upstox fee lookup
- if the Upstox token is stale, the shared client will auto-refresh it once through `./.venv/bin/python apps/journaling/upstox_token_refresh.py` and retry

Token refresh:

```bash
./.venv/bin/python apps/journaling/upstox_token_refresh.py --account ALL
```

## Walk-forward engine

```bash
cd /path/to/bala-trading-platform

./.venv/bin/python -m pip install -r apps/walk-forward/requirements.txt

./.venv/bin/python apps/walk-forward/main.py --list-profiles
./.venv/bin/python apps/walk-forward/main.py --profile-id silvermic_v3_default --dry-run
./.venv/bin/python apps/walk-forward/main.py --profile-id silvermic_v3_default
```

Replay:

```bash
./.venv/bin/python apps/walk-forward/replay.py --self-test

./.venv/bin/python apps/walk-forward/replay_batch.py --list-strategies
./.venv/bin/python apps/walk-forward/replay_batch.py --list-position-plans
```

## Legacy Upstox analyzers

Run the batch launcher:

```bash
cd /path/to/bala-trading-platform
zsh apps/analyzers-upstox/legacy/run_all.sh
```

Run individual analyzers:

```bash
python3.11 apps/analyzers-upstox/legacy/mcx_monitor/analyze_mcx.py

python3.11 apps/analyzers-upstox/legacy/stock_fo_monitor/analyze_stock_fo.py --mode weekly
python3.11 apps/analyzers-upstox/legacy/stock_fo_monitor/analyze_stock_fo.py --mode monthly

python3.11 apps/analyzers-upstox/legacy/stock_intraday_monitor/analyze_intraday.py

python3.11 apps/analyzers-upstox/legacy/index_expiry_monitor/sensex_expiry_short_straddle.py --analysis-only
```

Outputs:
- `data/legacy-analyzers/mcx-monitor/`
- `data/legacy-analyzers/stock-fo/`
- `data/legacy-analyzers/stock-intraday/`
- `data/legacy-analyzers/index-expiry/`

## Archive package only

```bash
cd /path/to/bala-trading-platform
PYTHONPATH=packages/trading_platform/src python3.11 -m trading_platform.cli init-db
```
