# MCX Monitor

Intraday MCX futures analyzer using Upstox quotes plus recent candles.

Tracked roots:
- `GOLDM`
- `SILVERMIC`
- `CRUDEOILM`
- `ZINCMINI`
- `NATGASMINI`

## Run

From the repo root:

```bash
python3.11 apps/analyzers-upstox/legacy/mcx_monitor/analyze_mcx.py
```

Optional overrides:

```bash
python3.11 apps/analyzers-upstox/legacy/mcx_monitor/analyze_mcx.py \
  --env-file /path/to/.env \
  --output-dir data/legacy-analyzers/mcx-monitor \
  --max-risk-rupees 2000
```

## Outputs

- `data/legacy-analyzers/mcx-monitor/latest_report.md`
- `data/legacy-analyzers/mcx-monitor/state.json`

## Notes

- repo defaults now point to the monorepo-local `.env` and output folders
- the included plist is a historical template; review paths before loading it

