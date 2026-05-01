# Historical Walk-Forward LLM Handover

This file preserves detailed build history and migration context from the earlier local workspace.
Some commands and paths are historical.
Use `apps/walk-forward/README.md` and `docs/operations/runbook.md` for the current public-facing instructions.

# Walk-Forward LLM Handover

This file is the quickest way for another LLM to pick up the `SILVERMIC` walk-forward validator without re-discovering the same runtime issues.

Longer-term architecture and milestone plan:
- [ROADMAP.md](/Users/rugan/balas-product-os/Tools/walk_forward/ROADMAP.md)
- [MILESTONE_1_DESIGN.md](/Users/rugan/balas-product-os/Tools/walk_forward/MILESTONE_1_DESIGN.md)

## Scope

Folder:
- [/Users/rugan/balas-product-os/Tools/walk_forward](/Users/rugan/balas-product-os/Tools/walk_forward)

Purpose:
- paper-trade the `SILVERMIC V3 CPR Band TC/BC Rejection` strategy during live MCX hours
- log trades to Notion
- send Telegram alerts
- never place live broker orders

Current maturity:
- operational as a **paper validator with first strategy/provider boundaries**
- not yet fully strategy-agnostic or broker-agnostic
- roadmap for that transition is documented separately in [ROADMAP.md](/Users/rugan/balas-product-os/Tools/walk_forward/ROADMAP.md)

Current architecture state as of `2026-04-18`:
- `main.py` now builds components through [runtime.py](/Users/rugan/balas-product-os/Tools/walk_forward/runtime.py)
- live runner profiles are registered in [runner_profiles.py](/Users/rugan/balas-product-os/Tools/walk_forward/runner_profiles.py)
- normalized models are in [models.py](/Users/rugan/balas-product-os/Tools/walk_forward/models.py)
- protocol interfaces are in [interfaces.py](/Users/rugan/balas-product-os/Tools/walk_forward/interfaces.py)
- Upstox is wrapped by [upstox_provider.py](/Users/rugan/balas-product-os/Tools/walk_forward/upstox_provider.py)
- the current rules are wrapped as [silvermic_v3_strategy.py](/Users/rugan/balas-product-os/Tools/walk_forward/silvermic_v3_strategy.py)
- paper execution is wrapped by [paper_position_manager.py](/Users/rugan/balas-product-os/Tools/walk_forward/paper_position_manager.py)
- strategy selection is through [strategy_registry.py](/Users/rugan/balas-product-os/Tools/walk_forward/strategy_registry.py)
- position lifecycle selection is through [position_plans.py](/Users/rugan/balas-product-os/Tools/walk_forward/position_plans.py)
- replay event processing is in [event_loop.py](/Users/rugan/balas-product-os/Tools/walk_forward/event_loop.py)
- replay provider/CSV loading is in [replay_provider.py](/Users/rugan/balas-product-os/Tools/walk_forward/replay_provider.py)
- replay metrics/persistence is in [replay_results.py](/Users/rugan/balas-product-os/Tools/walk_forward/replay_results.py)
- replay CLI is [replay.py](/Users/rugan/balas-product-os/Tools/walk_forward/replay.py)
- batch replay/experiment CLI is [replay_batch.py](/Users/rugan/balas-product-os/Tools/walk_forward/replay_batch.py)
- registered strategies:
  - `silvermic_cpr_band_v3`
  - `silvermic_cpr_breakout_v1`
- registered live runner profiles:
  - `silvermic_v3_default`
  - `silvermic_breakout_research`
- registered position plans:
  - `partial_t1_trail`
  - `full_t1_exit`
  - `single_lot_t1_exit`
- default profile id: `silvermic_v3_default`
- default strategy id: `silvermic_cpr_band_v3`
- default position plan id: `partial_t1_trail`

## Strategy summary

- instrument: `SILVERMIC` front-month futures
- session: `17:00–23:00 IST`
- timeframe: `15m`
- long setup: 2nd touch of `BC`
- short setup: 2nd touch of `TC`
- touch tolerance: `0.15%`
- min bars between touches: `3`
- SL: `SuperTrend(5,3)` or fallback `±0.8%`
- T1:
  - long -> `TC`
  - short -> `BC`
- quantity: `2 lots`
- management:
  - exit `1 lot` at `T1`
  - trail remaining `1 lot` with `SuperTrend(5,1.5)`
- force close: `23:00 IST`
- max trades/day: `2`

## Important runtime fixes already applied

### 0. Phase 2 strategy/provider boundary slice

What changed:
- added normalized `InstrumentRef`, `Candle`, `Quote`, and `DayContext`
- added protocol interfaces for market data, strategy, position management, journal, and alerts
- wrapped the existing Upstox feed instead of rewriting it
- wrapped the existing `SignalDetector` as `SilvermicCprBandV3Strategy`
- wrapped the existing `TradeManager` as a paper position manager
- added a small strategy registry
- changed `main.py` to use `build_runtime()`

What did not change:
- strategy rules
- signal timing
- SL/T1/trail behavior
- Notion field mapping
- Telegram message formatting
- paper-only safety posture

Validation done:
- AST syntax check passed for all `walk_forward/*.py`
- import/runtime smoke test selected `silvermic_cpr_band_v3`
- CLI help shows `--strategy-id`

### 0b. Position lifecycle pluggability slice

What changed:
- added `PositionPlan`
- added a position-plan registry/factory
- default plan id is `partial_t1_trail`
- `TradeManager` now uses the selected plan for:
  - total lots
  - T1 exit lots
  - lot size
  - fee per lot
  - trail-after-T1 behavior
- `main.py` supports `--position-plan-id`
- `config.py` reads `WFV_POSITION_PLAN_ID`

What did not change:
- default behavior remains `2` lots total
- `1` lot is booked at T1
- the remaining `1` lot is trailed
- EOD force close remains enabled
- live broker execution was not added

Validation done:
- AST syntax check passed for all `walk_forward/*.py`
- runtime smoke selected `partial_t1_trail`
- synthetic lifecycle test entered, hit T1, trailed/closed, and calculated P&L from the plan

### 0c. Replay/backtest-compatible event loop slice

What changed:
- added `CandleEventLoop`
- added `ReplayDataProvider`
- added CSV candle loader
- added `replay.py`
- replay runs the same:
  - strategy registry
  - position plan registry
  - paper `TradeManager`
  - dry-run Notion/Telegram sinks

What did not change:
- live paper runner behavior
- SILVERMIC V3 rules
- position lifecycle defaults
- paper-only safety posture

Validation done:
- AST syntax check passed for all `walk_forward/*.py`
- `python replay.py --self-test` produced:
  - `1` signal
  - `1` paper entry
  - `1` closed trade
- `python replay.py --help` works

### 0d. Replay result persistence and metrics

What changed:
- added [replay_results.py](/Users/rugan/balas-product-os/Tools/walk_forward/replay_results.py)
- `replay.py` now writes:
  - JSON report
  - trades CSV
- default output folder:
  - `/Users/rugan/balas-product-os/Tools/walk_forward/output/replay`
- new CLI flags:
  - `--output-dir`
  - `--run-id`
  - `--no-save`

Metrics currently computed:
- candles processed
- signals seen
- entries taken
- closed trades
- wins / losses / breakeven
- win rate
- gross P&L
- net P&L
- average net P&L
- average win
- average loss
- profit factor
- expectancy
- average R
- total R
- max drawdown
- max consecutive losses

Validation done:
- `python replay.py --self-test --run-id smoke_metrics_test` wrote:
  - `/Users/rugan/balas-product-os/Tools/walk_forward/output/replay/smoke_metrics_test.json`
  - `/Users/rugan/balas-product-os/Tools/walk_forward/output/replay/smoke_metrics_test_trades.csv`
- JSON summary and CSV trade row were inspected successfully

### 0e. Batch replay experiment runner

What changed:
- added [replay_batch.py](/Users/rugan/balas-product-os/Tools/walk_forward/replay_batch.py)
- added [replay_batch_manifest.example.json](/Users/rugan/balas-product-os/Tools/walk_forward/replay_batch_manifest.example.json)
- refactored [replay.py](/Users/rugan/balas-product-os/Tools/walk_forward/replay.py) so the core replay execution can be reused by batch mode
- batch mode can run:
  - `--self-test`
  - all CSVs in a folder through `--csv-dir` and `--pattern`
  - manifest-defined runs through `--manifest`
- a failed run is captured as `status=failed` in the batch summary instead of stopping every remaining run

What did not change:
- live paper runner behavior
- SILVERMIC V3 rules
- default position lifecycle
- paper-only safety posture
- no live broker execution path was added

Batch outputs:
- per-run replay reports under:
  - `/Users/rugan/balas-product-os/Tools/walk_forward/output/replay_batch/<batch_id>/runs/`
- ranked batch files:
  - `batch_summary.csv`
  - `batch_summary.json`

Ranking rule:
- sort by net P&L descending
- then average R descending
- then max drawdown ascending

Validation done:
- AST syntax check passed for all `walk_forward/*.py`
- `python replay_batch.py --help` works
- `python replay_batch.py --self-test --batch-id smoke_batch_test` wrote:
  - `/Users/rugan/balas-product-os/Tools/walk_forward/output/replay_batch/smoke_batch_test/batch_summary.csv`
  - `/Users/rugan/balas-product-os/Tools/walk_forward/output/replay_batch/smoke_batch_test/batch_summary.json`
  - `/Users/rugan/balas-product-os/Tools/walk_forward/output/replay_batch/smoke_batch_test/runs/self_test.json`
  - `/Users/rugan/balas-product-os/Tools/walk_forward/output/replay_batch/smoke_batch_test/runs/self_test_trades.csv`

### 0f. Multi-strategy, multi-plan, and multi-session replay sweep

What changed:
- added [silvermic_cpr_breakout_strategy.py](/Users/rugan/balas-product-os/Tools/walk_forward/silvermic_cpr_breakout_strategy.py)
- strategy registry now exposes:
  - `silvermic_cpr_band_v3`
  - `silvermic_cpr_breakout_v1`
- position-plan registry now exposes:
  - `partial_t1_trail`
  - `full_t1_exit`
  - `single_lot_t1_exit`
- [replay_provider.py](/Users/rugan/balas-product-os/Tools/walk_forward/replay_provider.py) can enumerate replayable session dates from one CSV
- [replay_batch.py](/Users/rugan/balas-product-os/Tools/walk_forward/replay_batch.py) now supports:
  - `--csv`
  - `--all-session-dates`
  - `--date-from`
  - `--date-to`
  - `--strategy-ids`
  - `--position-plan-ids`
  - `--list-strategies`
  - `--list-position-plans`

What did not change:
- live paper runner behavior
- default `silvermic_cpr_band_v3` behavior
- paper-only safety posture
- no broker execution path was added

Validation done:
- `python replay_batch.py --list-strategies` shows both registered strategies
- `python replay_batch.py --list-position-plans` shows all three registered plans
- `python replay.py --self-test --strategy-id silvermic_cpr_breakout_v1 --position-plan-id full_t1_exit --run-id breakout_full_exit_smoke` wrote replay artifacts successfully
- `python replay_batch.py --self-test --strategy-ids silvermic_cpr_band_v3 silvermic_cpr_breakout_v1 --position-plan-ids partial_t1_trail full_t1_exit --batch-id strategy_plan_matrix_smoke` completed successfully
- `python replay_batch.py --csv /tmp/wfv_multi_session.csv --all-session-dates --strategy-ids silvermic_cpr_band_v3 silvermic_cpr_breakout_v1 --position-plan-ids partial_t1_trail full_t1_exit --batch-id multi_session_matrix_smoke` completed successfully and wrote:
  - `/Users/rugan/balas-product-os/Tools/walk_forward/output/replay_batch/multi_session_matrix_smoke/batch_summary.csv`
  - `/Users/rugan/balas-product-os/Tools/walk_forward/output/replay_batch/multi_session_matrix_smoke/batch_summary.json`

### 0g. Profile-driven multi-runner live paper orchestration

What changed:
- added [runner_profiles.py](/Users/rugan/balas-product-os/Tools/walk_forward/runner_profiles.py)
- `RunnerProfile` now supports:
  - `profile_id`
  - `instrument_key_prefix`
  - `strategy_id`
  - `position_plan_id`
  - `display_prefix`
  - `runner_label`
- `main.py` now supports:
  - `--profile-id` (repeatable)
  - `--list-profiles`
- when multiple profiles are selected, `main.py` launches one child process per profile
- each child process writes to its own log file under `~/Library/Logs/walk_forward/<profile_id>_YYYY-MM-DD.log`
- Upstox instrument discovery now respects a configured root symbol prefix instead of assuming only `SILVERMIC`

What did not change:
- default `python main.py` still runs one live paper validator
- broker execution was not added
- paper remains the only execution mode

Validation done:
- AST syntax check passed for all `walk_forward/*.py`
- `python main.py --list-profiles` shows:
  - `silvermic_breakout_research`
  - `silvermic_v3_default`
- `python main.py --help` shows the new profile CLI options

### 1. SILVERMIC contract auto-discovery

Problem that existed:
- older code relied on legacy Upstox instrument-master URLs that were returning `403`

Fix:
- [upstox_feed.py](/Users/rugan/balas-product-os/Tools/walk_forward/upstox_feed.py) now uses:
  - `https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz`
- this is the same working source already used in:
  - [/Users/rugan/Projects/upstox-analyzer/mcx_monitor/analyze_mcx.py](/Users/rugan/Projects/upstox-analyzer/mcx_monitor/analyze_mcx.py)

Details:
- resolver filters `MCX_FO` + `FUT/FUTCOM` + `underlying_symbol/name/trading_symbol` matching `SILVERMIC`
- expiry normalization supports:
  - ISO dates
  - epoch milliseconds
- nearest non-expired contract is selected automatically

Manual override:
- still supported via:
  - `UPSTOX_SILVERMIC_KEY`
  - `UPSTOX_SILVERMIC_SYMBOL`
  - `UPSTOX_SILVERMIC_EXPIRY`
- but now only used as fallback

### 2. Historical candles alignment

Problem that existed:
- walk-forward was still using older `v2` historical paths
- Upstox rejected the live MCX key there during testing

Fix:
- feed now uses `v3/historical-candle` consistently:
  - daily: `days/1`
  - intraday: `minutes/15`
  - warmup: `minutes/15`

### 3. Day summary capture

Problem that existed:
- `main.py` had `_capture_closed_trade()` as `pass`
- summary could be empty/wrong even if trades closed correctly

Fix:
- [trade_manager.py](/Users/rugan/balas-product-os/Tools/walk_forward/trade_manager.py)
  - stores closed trades
- [main.py](/Users/rugan/balas-product-os/Tools/walk_forward/main.py)
  - pops newly closed trades into `self._day_trades`
  - captures EOD force-closed trades too

### 4. Notion database ID configurability

Fix:
- [config.py](/Users/rugan/balas-product-os/Tools/walk_forward/config.py) now reads:
  - `NOTION_WF_DB_ID`
- default remains:
  - operator-supplied Notion database ID from env

## Verified smoke test

Date:
- `April 15, 2026`

What was tested:
- synthetic paper-only trade using the real runtime stack:
  - auto instrument discovery
  - previous-day OHLC fetch
  - LTP fetch
  - Notion create
  - Notion update on close
  - Telegram signal/close path

Result:
- success

Resolved contract:
- instrument key: `MCX_FO|466029`
- trading symbol: `SILVERMIC FUT 30 APR 26`
- expiry: `2026-04-30`

Synthetic trade details:
- entry: `252909.0`
- exit: `252934.0`
- gross P&L: `50.0`
- net P&L: `-50.0`

Notion page created:
- `343d2aac-5f9d-8174-8622-e1e21727fbae`

This page is disposable and can be deleted manually from Notion.

## Environment requirements

Expected in:
- [/Users/rugan/balas-product-os/.env](/Users/rugan/balas-product-os/.env)

Required:
- `UPSTOX_ACCESS_TOKEN`
- `UPSTOX_API_KEY`
- `NOTION_API_KEY`
- `NOTION_WF_DB_ID`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Safety notes

- this validator is paper-only
- there is no broker order placement path in the current implementation
- `--dry-run` uses mocks for Notion and Telegram
- if you need to validate real journaling/alerts, do **not** use `--dry-run`

## Run command

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python main.py
```

Optional dry-run:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python main.py --dry-run
```

Optional explicit strategy selection:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python main.py --strategy-id silvermic_cpr_band_v3
```

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python main.py --strategy-id silvermic_cpr_breakout_v1
```

List and run live profiles:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python main.py --list-profiles
python main.py --profile-id silvermic_v3_default
python main.py --profile-id silvermic_breakout_research
```

Launch multiple profiles together:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python main.py \
  --profile-id silvermic_v3_default \
  --profile-id silvermic_breakout_research \
  --dry-run
```

Optional explicit position-plan selection:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python main.py --position-plan-id partial_t1_trail
```

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python main.py --position-plan-id full_t1_exit
```

Bounded live-data health test:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python main.py --dry-run --test-duration-minutes 60 --ignore-session-window
```

Notes:
- this polls live Upstox data immediately, even outside `17:00-23:00 IST`
- it is for health checks only
- `--dry-run` avoids Notion writes and Telegram sends
- production paper validation should still use plain `python main.py`

Replay smoke test:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python replay.py --self-test
```

Replay from CSV:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python replay.py \
  --csv /path/to/candles.csv \
  --session-date 2026-04-17 \
  --trading-symbol "SILVERMIC REPLAY"
```

Replay with explicit artifact name:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python replay.py --self-test --run-id smoke_metrics_test
```

List registered strategies and position plans:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python replay_batch.py --list-strategies
python replay_batch.py --list-position-plans
```

Strategy x position-plan matrix smoke test:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python replay_batch.py \
  --self-test \
  --strategy-ids silvermic_cpr_band_v3 silvermic_cpr_breakout_v1 \
  --position-plan-ids partial_t1_trail full_t1_exit \
  --batch-id strategy_plan_matrix_smoke
```

Multi-session sweep from one CSV:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python replay_batch.py \
  --csv /tmp/wfv_multi_session.csv \
  --all-session-dates \
  --strategy-ids silvermic_cpr_band_v3 silvermic_cpr_breakout_v1 \
  --position-plan-ids partial_t1_trail full_t1_exit \
  --batch-id multi_session_matrix_smoke
```

CSV requirements:
- required columns: `timestamp`, `open`, `high`, `low`, `close`
- optional columns: `volume`, `oi`
- timestamps can include timezone offsets; timezone-less timestamps are treated as IST
- previous-day OHLC is inferred from candles before `--session-date`, or can be passed manually

Log tail:

```bash
tail -f ~/Library/Logs/walk_forward/wfv_$(date +%F).log
```

## Tomorrow go-live checklist

Before session:
- token valid
- Notion integration still has DB access
- Telegram bot chat active
- `.env` values present

At startup:
- verify log lines for:
  - instrument resolution
  - previous-day OHLC
  - warm-up candles
  - CPR summary

During session:
- signal -> Notion create + Telegram signal
- close -> Notion update + Telegram close
- EOD -> Telegram summary

## Best next debugging order if something breaks

1. inspect log:
   - `~/Library/Logs/walk_forward/wfv_YYYY-MM-DD.log`
2. verify Upstox token
3. verify Notion integration permissions
4. verify Telegram bot token/chat id
5. only use manual `UPSTOX_SILVERMIC_*` override if auto-discovery suddenly fails again

## Next safe architecture step

The next milestone should move from multi-runner paper orchestration to execution abstraction:
- add a parity checklist comparing replay behavior with the live paper path
- promote more than one real instrument profile once live paper sessions prove stable
- extract a dedicated execution-provider boundary while keeping paper as the only enabled mode
- keep broker execution disabled until replay and paper parity are explicitly reviewed
