# trading_platform Package

Shared package for the broker-agnostic platform direction.

Current focus:
- local archive bootstrap
- brief prediction storage
- brief outcome storage
- live-analysis storage
- shared repo path helpers

## Package layout

```text
packages/trading_platform/src/trading_platform/
  archive/
  briefs/
  cli.py
  paths.py
```

## Initialize the archive

```bash
cd /path/to/bala-trading-platform
PYTHONPATH=packages/trading_platform/src python3.11 -m trading_platform.cli init-db
```

## Default local data locations

- archive DB: `data/archive/platform.sqlite3`
- report outputs: `data/reports/`

## Historical note

The older design/context snapshot is preserved in `README_SOURCE.md`.

