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

## Notes

- same-day processing is preferred because Upstox same-day trades include better timestamp context
- broker XLSX backfill is the fallback path when the same-day run was missed
- older deep-reference notes were preserved in `TOOLS_README_SOURCE.md`
