# Legacy Upstox Analyzers

This folder preserves the earlier analyzer suite in a cleaner monorepo layout.

It includes:
- MCX intraday futures analyzer
- stock F&O weekly / monthly analyzer
- stock intraday analyzer
- Sensex expiry monitoring scripts

These are called “legacy” because they were built before the current shared platform package and broker-agnostic architecture work. They are still useful and runnable.

## Batch run

From the repo root:

```bash
cd /path/to/bala-trading-platform
zsh apps/analyzers-upstox/legacy/run_all.sh
```

If your env file is elsewhere:

```bash
BALA_TRADING_ENV_FILE=/path/to/.env zsh apps/analyzers-upstox/legacy/run_all.sh
```

## Output folders

- `data/legacy-analyzers/mcx-monitor/`
- `data/legacy-analyzers/stock-fo/`
- `data/legacy-analyzers/stock-intraday/`
- `data/legacy-analyzers/index-expiry/`

## Subprojects

- `mcx_monitor/`
- `stock_fo_monitor/`
- `stock_intraday_monitor/`
- `index_expiry_monitor/`

## Historical handoff material

Older deep-reference notes are preserved here:
- `LLM_HANDOVER.md`
- `SESSION_NOTES.md`
- `HANDOFF_PROMPT.txt`

Those files are useful for hard-won implementation context, but some of their commands still point to the earlier private local workspace. Use this README plus `docs/operations/runbook.md` for the current public-facing commands.

