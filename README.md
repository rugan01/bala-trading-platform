# Bala Trading Platform

Public monorepo for Bala's reusable trading tooling.

This repository is the code-centric source of truth for the reusable trading stack:
- journaling and recovery
- morning brief / live analysis / EOD learning loop
- walk-forward paper validation
- legacy Upstox analyzers
- shared archive and platform package
- backtesting research assets

## Current Status

This repo is the **organized monorepo** for the reusable codebase.

There is still a separate operational workspace used for day-to-day trading operations and notes. The intended split is:
- reusable code lives here
- secrets stay local and are not committed
- generated outputs stay local and are not committed
- operational reports and private trading context stay outside the public repo

## Layout

```text
.
  apps/
    briefing/
    journaling/
    walk-forward/
    analyzers-upstox/legacy/
  packages/
    trading_platform/
  research/
    backtesting/
  docs/
    architecture/
    inventory/
    operations/
  ops/
    launchd/
  data/
    archive/
    reports/
    legacy-analyzers/
```

## Quick Start

1. Create a local env file:

```bash
cp .env.example .env
```

2. Initialize the archive database:

```bash
cd /path/to/bala-trading-platform
PYTHONPATH=packages/trading_platform/src python3.11 -m trading_platform.cli init-db
```

3. Example commands:

```bash
python3.11 apps/briefing/morning_brief.py
python3.11 apps/briefing/live_analysis.py
python3.11 apps/briefing/brief_eod_review.py --source-date YYYY-MM-DD

python3.11 apps/journaling/trade_journaling.py --account BALA
python3.11 apps/journaling/broker_trade_backfill.py --broker-file /path/to/export.xlsx --date YYYY-MM-DD

python3.11 apps/walk-forward/main.py --profile-id silvermic_v3_default --dry-run

zsh apps/analyzers-upstox/legacy/run_all.sh
```

## Documentation

Start here:
- `docs/inventory/tooling-inventory.md`
- `docs/operations/maintenance-map.md`
- `docs/operations/runbook.md`
- `docs/architecture/`

## Secrets and Exclusions

Do not commit:
- `.env`
- broker exports
- SQLite archives
- generated reports
- virtualenvs
- logs

These are already covered by `.gitignore`, but the operating assumption should still be that credentials and outputs remain local-only.
