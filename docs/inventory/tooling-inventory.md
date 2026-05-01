# Tooling Inventory

This file is the high-level map of what lives in the staging monorepo and where it came from.

## Core Application Groups

### `apps/briefing`

Purpose:
- morning market brief
- live thesis check
- EOD prediction review
- F&O scanner
- MCX market analysis helpers

Key entry points:
- `apps/briefing/morning_brief.py`
- `apps/briefing/live_analysis.py`
- `apps/briefing/brief_eod_review.py`
- `apps/briefing/fno_scanner.py`
- `apps/briefing/mcx_market_analysis.py`

Source history:
- migrated from `/Users/rugan/balas-product-os/Tools`

### `apps/journaling`

Purpose:
- same-day trade journaling
- historical trade recovery
- broker XLSX fallback backfill
- Upstox access-token refresh

Key entry points:
- `apps/journaling/trade_journaling.py`
- `apps/journaling/broker_trade_backfill.py`
- `apps/journaling/upstox_token_refresh.py`
- `apps/journaling/journal_keys.py`

Source history:
- migrated from `/Users/rugan/balas-product-os/Tools`

### `apps/walk-forward`

Purpose:
- paper validation engine
- replay / batch replay
- strategy registry and position-plan experiments

Key entry points:
- `apps/walk-forward/main.py`
- `apps/walk-forward/replay.py`
- `apps/walk-forward/replay_batch.py`

Source history:
- migrated from `/Users/rugan/balas-product-os/Tools/walk_forward`

### `apps/analyzers-upstox/legacy`

Purpose:
- MCX analyzer
- stock F&O weekly/monthly analyzer
- stock intraday analyzer
- Sensex expiry monitors

Key entry points:
- `apps/analyzers-upstox/legacy/run_all.sh`
- `apps/analyzers-upstox/legacy/mcx_monitor/analyze_mcx.py`
- `apps/analyzers-upstox/legacy/stock_fo_monitor/analyze_stock_fo.py`
- `apps/analyzers-upstox/legacy/stock_intraday_monitor/analyze_intraday.py`

Source history:
- migrated from `/Users/rugan/Projects/upstox-analyzer`

### `packages/trading_platform`

Purpose:
- shared archive bootstrap
- learning-loop persistence
- reusable platform data model

Key entry points:
- `packages/trading_platform/src/trading_platform/cli.py`
- `packages/trading_platform/src/trading_platform/archive/`
- `packages/trading_platform/src/trading_platform/briefs/`

## Research and Docs

### `research/backtesting`

Purpose:
- backtesting assets and strategy research files

### `docs/architecture`

Purpose:
- roadmap and design notes

### `docs/operations`

Purpose:
- maintenance and migration guidance

## Current Runtime Data Layout

Generated local-only paths inside this repo:
- `data/archive/platform.sqlite3`
- `data/reports/premarket/`
- `data/legacy-analyzers/`

These are intentionally excluded from version control.

