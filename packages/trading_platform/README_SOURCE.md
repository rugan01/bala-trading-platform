# Historical Source Snapshot

This file is an archived source snapshot from the earlier local workspace.

It is preserved for design history and context. Some commands and paths in this file still point to the older private local setup and should not be treated as the primary public-facing instructions.

# Trading Platform

Broker-agnostic trading platform scaffold for:
- market-data archival
- instrument reference storage
- strategy replay / forward testing
- execution and risk orchestration
- journaling and reconciliation
- monitoring and dashboard projections

This project is the next-stage evolution of the current script-based tooling under:
- `/Users/rugan/balas-product-os/Tools/`
- `/Users/rugan/balas-product-os/Projects/trading-system/`

## Current Focus

Phase 1 is intentionally narrow:
- create the local archive foundation
- store canonical instrument and fill references
- prepare structured storage for morning-brief predictions and learning-loop evaluation

## Proposed Structure

```text
Projects/trading-platform/
  README.md
  docs/
    morning-brief-learning-loop.md
  src/trading_platform/
    archive/
    briefs/
    cli.py
  data/
    archive/
```

## First Useful Commands

Initialize the local archive database:

```bash
cd /Users/rugan/balas-product-os/Projects/trading-platform
PYTHONPATH=src python3.11 -m trading_platform.cli init-db
```

Compile-check the scaffold:

```bash
cd /Users/rugan/balas-product-os/Projects/trading-platform
python3.11 -m py_compile $(find src -name "*.py")
```

## First usable Python surface

```python
from trading_platform.briefs import BriefPredictionRecord, BriefRunRecord, archive_brief_run

run = BriefRunRecord(
    run_timestamp="2026-04-30T09:20:00+05:30",
    run_date="2026-04-30",
    session_label="morning_brief",
    mode="manual",
    market_phase="intraday",
    summary_text="Nifty fading gap-up; relative strength still visible in selected pharma names.",
)

predictions = [
    BriefPredictionRecord(
        asset_class="equity",
        universe="NSE_FNO",
        symbol="SUNPHARMA",
        timeframe="intraday",
        horizon_label="EOD",
        signal_family="relative_strength",
        predicted_direction="bullish",
        confidence_score=0.68,
        recommendation_text="Look for long continuation only above first 30-minute high.",
    )
]

archive_brief_run(run, predictions)
```

Default archive path:
- `/Users/rugan/balas-product-os/Projects/trading-platform/data/archive/platform.sqlite3`

## Why the morning brief is part of phase 1

The brief is one of the highest-leverage decision tools you already use daily. By turning each run into a structured, scored prediction set, we get immediate feedback loops without waiting for the full execution stack to be finished.

See the design note here:
- `/Users/rugan/balas-product-os/Projects/trading-platform/docs/morning-brief-learning-loop.md`


## Operational Commands

Run the morning brief with archive-backed structured output:

```bash
python3.11 /Users/rugan/balas-product-os/Tools/morning_brief.py
```

This now writes:
- human-readable report text
- structured JSON sidecar
- archived `brief_runs` and `brief_predictions`

Default report outputs:
- `/Users/rugan/balas-product-os/Projects/trading-system/premarket/reports/morning_brief_latest.txt`
- `/Users/rugan/balas-product-os/Projects/trading-system/premarket/reports/morning_brief_latest.json`

Run the intraday thesis check against the archived morning brief:

```bash
python3.11 /Users/rugan/balas-product-os/Tools/live_analysis.py
```

This writes:
- live comparison text report
- structured JSON sidecar
- archived `live_analysis_runs` and `live_analysis_checks`

Default live-analysis outputs:
- `/Users/rugan/balas-product-os/Projects/trading-system/premarket/reports/live/live_analysis_latest.txt`
- `/Users/rugan/balas-product-os/Projects/trading-system/premarket/reports/live/live_analysis_latest.json`

Run the end-of-day review so the next morning's learning summary has real outcomes:

```bash
python3.11 /Users/rugan/balas-product-os/Tools/brief_eod_review.py --source-date YYYY-MM-DD
```

This writes directional outcome rows into `brief_outcomes` for currently supported calls.

## Current limitation

The F&O stock scanner now defaults to Upstox for both EOD and live modes. Global-markets style context can still use Yahoo where Upstox is not the right source, but the stock and index brief workflow is now Upstox-first.
