# Journaling

This app group handles:
- same-day trade journaling from Upstox
- historical recovery using the correct Upstox historical-trades path
- broker XLSX fallback backfill
- stable journal dedupe keys
- Upstox token refresh utilities

## Main entry points

- `trade_journaling.py`
- `broker_trade_backfill.py`
- `upstox_token_refresh.py`
- `journal_keys.py`

## Install dependencies

```bash
cd /path/to/bala-trading-platform
python3.11 -m pip install -r apps/journaling/requirements.txt
```

For `upstox_token_refresh.py`, use Python `3.12+` because `upstox-totp` does not support Python `3.11`.
A simple setup is:

```bash
python3.13 -m venv .venv
./.venv/bin/python -m pip install -r apps/journaling/requirements.txt
```

## Common commands

```bash
cd /path/to/bala-trading-platform

python3.11 apps/journaling/trade_journaling.py --account BALA
python3.11 apps/journaling/trade_journaling.py --account NIMMY

python3.11 apps/journaling/trade_journaling.py --date YYYY-MM-DD --account BALA --dry-run

python3.11 apps/journaling/broker_trade_backfill.py \
  --broker-file /path/to/export.xlsx \
  --date YYYY-MM-DD

./.venv/bin/python apps/journaling/upstox_token_refresh.py --account ALL
```

## Configuration

These scripts read from the repo-local `.env` by default.

Important values:
- `UPSTOX_*`
- `NOTION_API_KEY`
- `NOTION_TRADING_JOURNAL_DB`
- optional `BROKER_UCC_BALA`
- optional `BROKER_UCC_NIMMY`

For broker XLSX recovery, set the UCC mapping in `.env` if you want account
auto-detection without passing `--account`:

```bash
BROKER_UCC_BALA=...
BROKER_UCC_NIMMY=...
```

## Broker recovery behavior

When the same-day journal run was missed, the preferred recovery path is:

```bash
python3.11 apps/journaling/broker_trade_backfill.py \
  --broker-file /path/to/export.xlsx \
  --date YYYY-MM-DD
```

What this path does:
- auto-detects the account from `BROKER_UCC_*` in `.env` when available
- keeps the exact broker-file trade times instead of writing `00:00`
- replays the fills through the shared journal processor so direction, FIFO matching, and spread handling stay consistent
- uses Upstox fee lookup for brokerage / charges
- if Upstox returns `401` during historical fetch or fee lookup, attempts one automatic token refresh through the repo `.venv` token-refresh script and retries

If account auto-detection is not configured yet, pass the account explicitly:

```bash
python3.11 apps/journaling/broker_trade_backfill.py \
  --broker-file /path/to/export.xlsx \
  --date YYYY-MM-DD \
  --account BALA
```

## Notes

- same-day processing is preferred because Upstox same-day trades include better timestamp context
- broker XLSX backfill is the fallback path when the same-day run was missed
- broker XLSX backfill preserves the broker-file trade times and uses Upstox for fee lookup
- if the Upstox token is stale during historical recovery or fee lookup, the shared client will attempt one automatic refresh via the repo `.venv` token-refresh script and retry
- older deep-reference notes were preserved in `TOOLS_README_SOURCE.md`
