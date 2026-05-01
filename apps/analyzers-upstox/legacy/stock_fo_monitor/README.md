# Stock F&O Analyzer

Weekly and monthly analyzer for Nifty F&O stocks plus key indices.

Features:
- trend and MA stack analysis
- RSI / ADX / DI checks
- 52-week high / low proximity
- relative strength vs Nifty benchmarks
- option chain and spread suggestion logic
- index structure suggestions

## Run

Weekly mode:

```bash
python3.11 apps/analyzers-upstox/legacy/stock_fo_monitor/analyze_stock_fo.py --mode weekly
```

Monthly mode:

```bash
python3.11 apps/analyzers-upstox/legacy/stock_fo_monitor/analyze_stock_fo.py --mode monthly
```

Optional overrides:

```bash
python3.11 apps/analyzers-upstox/legacy/stock_fo_monitor/analyze_stock_fo.py \
  --env-file /path/to/.env \
  --universe-file apps/analyzers-upstox/legacy/stock_fo_monitor/universe_nifty_fo.txt \
  --output-dir data/legacy-analyzers/stock-fo \
  --mode weekly \
  --top 8
```

## Outputs

- `data/legacy-analyzers/stock-fo/weekly_report.md`
- `data/legacy-analyzers/stock-fo/monthly_report.md`

