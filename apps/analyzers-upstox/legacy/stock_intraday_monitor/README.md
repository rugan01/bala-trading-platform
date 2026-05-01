# Stock Intraday Analyzer

Intraday analyzer for Nifty F&O stocks and key indices.

Focus:
- strongest / weakest intraday names
- relative intraday strength vs Nifty
- narrow CPR expansion filter
- 15-minute trade-plan generation

## Run

```bash
python3.11 apps/analyzers-upstox/legacy/stock_intraday_monitor/analyze_intraday.py
```

Optional overrides:

```bash
python3.11 apps/analyzers-upstox/legacy/stock_intraday_monitor/analyze_intraday.py \
  --env-file /path/to/.env \
  --universe-file apps/analyzers-upstox/legacy/stock_fo_monitor/universe_nifty_fo.txt \
  --output-dir data/legacy-analyzers/stock-intraday \
  --top 8
```

## Output

- `data/legacy-analyzers/stock-intraday/intraday_report.md`

