# SILVERMIC Walk-Forward Validator

Paper-trades the CPR Band TC/BC Rejection strategy on SILVERMIC during live MCX market hours. All trades are auto-logged to the **Trading Journal Walk forward** Notion database.

Current status as of **April 27, 2026**:
- automated `SILVERMIC` front-month contract discovery is working again
- Notion create + update path is verified
- Telegram signal/close path is enabled and exercised in a paper-only smoke test
- the validator remains **paper-only**; it does **not** place live broker orders
- the research/runtime slice now supports:
  - normalized models and protocol interfaces
  - Upstox market-data provider wrapper
  - `SilvermicCprBandV3Strategy` strategy adapter
  - `SilvermicCprBreakoutStrategy` strategy adapter
  - paper position-manager adapter
  - strategy registry with `silvermic_cpr_band_v3` and `silvermic_cpr_breakout_v1`
  - position plan registry with `partial_t1_trail`, `full_t1_exit`, and `single_lot_t1_exit`
  - replay/backtest-compatible candle event loop
  - runtime composition via `build_runtime()`
  - replay metrics with JSON/CSV artifacts
  - batch replay/experiment runner with ranked summary outputs
  - multi-session replay sweeps from one CSV plus strategy/plan matrix experiments
  - profile-driven live paper runners with isolated child-process logs

Related handoff notes:
- [LLM_HANDOVER.md](/Users/rugan/balas-product-os/Tools/walk_forward/LLM_HANDOVER.md)
- [ROADMAP.md](/Users/rugan/balas-product-os/Tools/walk_forward/ROADMAP.md)
- [MILESTONE_1_DESIGN.md](/Users/rugan/balas-product-os/Tools/walk_forward/MILESTONE_1_DESIGN.md)

---

## Strategy Rules (V3)

| Parameter | Value |
|-----------|-------|
| Instrument | MCX SILVERMIC (front-month futures) |
| Session | 17:00 – 23:00 IST |
| Timeframe | 15 minutes |
| HTF Filter | OFF |
| Long entry | 2nd touch of BC (Bottom Central) |
| Short entry | 2nd touch of TC (Top Central) |
| Touch tolerance | 0.15% proximity |
| Min bars between touches | 3 bars |
| SL | SuperTrend(5, 3.0) or BC/TC ± 0.8% fallback |
| T1 (50% exit) | TC for longs, BC for shorts |
| Trailing (50%) | SuperTrend(5, 1.5) after T1 hit |
| Force close | 23:00 IST |
| Max trades/day | 2 |
| Position size | 2 lots (1 kg each) |

---

## Prerequisites

### 1. Python environment
Uses the same venv as the token refresh script:
```bash
cd ~/balas-product-os/Tools
source .venv/bin/activate
pip install -r walk_forward/requirements.txt
```

### 2. Environment variables (add to your local repo `.env`)
```env
# Already present
UPSTOX_ACCESS_TOKEN=...
UPSTOX_API_KEY=...

# New — add these
NOTION_API_KEY=secret_xxxxxxxxxxxxxxxxxxxx
NOTION_WF_DB_ID=<your_notion_database_id>
TELEGRAM_BOT_TOKEN=xxxxxxxxxx:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
TELEGRAM_CHAT_ID=xxxxxxxxxx
```

**How to get Telegram credentials:**
1. Message @BotFather on Telegram → `/newbot` → copy the token
2. Send a message to your new bot, then visit:
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   and copy the `chat.id` value

**How to get Notion API key:**
1. Go to https://www.notion.so/my-integrations
2. Create a new integration → copy the Internal Integration Token
3. Open the "Trading Journal Walk forward" database → Share → Invite your integration

---

## Running

### Manual (one-off)
```bash
cd ~/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate

# Real paper-trading run
python main.py

# Dry run (no Notion writes, no Telegram messages — just logs)
python main.py --dry-run

# Inspect registered live runner profiles
python main.py --list-profiles

# Run an explicit profile
python main.py --profile-id silvermic_v3_default
python main.py --profile-id silvermic_breakout_research

# Launch multiple profiles together
python main.py --profile-id silvermic_v3_default --profile-id silvermic_breakout_research --dry-run

# Optional explicit strategy selection
python main.py --strategy-id silvermic_cpr_band_v3
python main.py --strategy-id silvermic_cpr_breakout_v1

# Optional explicit position-plan selection
python main.py --position-plan-id partial_t1_trail
python main.py --position-plan-id full_t1_exit
python main.py --position-plan-id single_lot_t1_exit

# One-hour live-data health test outside the normal strategy session
python main.py --dry-run --test-duration-minutes 60 --ignore-session-window

# Replay smoke test using deterministic synthetic candles
python replay.py --self-test

# Inspect available strategy and position-plan ids
python replay_batch.py --list-strategies
python replay_batch.py --list-position-plans
```

### Recommended command for tomorrow
```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python main.py
```

### Optional live log tail
```bash
tail -f ~/Library/Logs/walk_forward/wfv_$(date +%F).log
```

### Bounded live-data health test

Use this when you want to check the runtime with current live candles without
waiting for the configured `17:00-23:00 IST` strategy window:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate
python main.py --dry-run --test-duration-minutes 60 --ignore-session-window
```

Safety:
- `--dry-run` prevents Notion writes and Telegram sends
- `--test-duration-minutes 60` auto-stops after one hour
- `--ignore-session-window` is for health checks only and should not be used for normal paper validation
- normal `python main.py` still respects the strategy session window
- when more than one `--profile-id` is supplied, `main.py` launches one child process per profile for clean log/state isolation

### Replay / backtest-compatible runner

Use replay mode to run the same registered strategy and position plan over
historical or synthetic candles without live market APIs:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate

# Built-in deterministic smoke test
python replay.py --self-test

# Replay from CSV
python replay.py \
  --csv /path/to/candles.csv \
  --session-date 2026-04-17 \
  --trading-symbol "SILVERMIC REPLAY"
```

CSV format:

```csv
timestamp,open,high,low,close,volume,oi
2026-04-16T17:00:00+05:30,250000,251000,249500,250700,1000,0
2026-04-17T17:00:00+05:30,250700,251200,250200,250950,1200,0
```

Notes:
- `timestamp`, `open`, `high`, `low`, and `close` are required
- `volume` and `oi` are optional
- if the CSV contains candles before `--session-date`, previous-day OHLC is inferred automatically
- alternatively pass all of `--prev-open`, `--prev-high`, `--prev-low`, and `--prev-close`
- replay uses dry-run sinks, so it does not create Notion pages or send Telegram alerts
- replay saves artifacts by default under `walk_forward/output/replay/`
- use `--output-dir /path/to/output` to change the artifact folder
- use `--run-id my_run_name` to control artifact filenames
- use `--no-save` to print metrics without writing files

Replay artifacts:
- `<run_id>.json` contains run config, summary metrics, and full serialized trades
- `<run_id>_trades.csv` contains one row per closed trade

Current summary metrics:
- candles processed
- signals seen
- entries taken
- closed trades
- wins, losses, breakeven
- win rate
- gross and net P&L
- average net P&L
- average win and average loss
- profit factor
- expectancy
- average R and total R
- max drawdown
- max consecutive losses

### Batch replay / experiment runner

Use batch replay when you want to compare several sessions, CSVs, or future
strategy/position-plan variants in one command:

```bash
cd /Users/rugan/balas-product-os/Tools/walk_forward
source ../.venv/bin/activate

# Built-in batch smoke test
python replay_batch.py --self-test --batch-id smoke_batch_test

# Replay every CSV in a folder
python replay_batch.py \
  --csv-dir /path/to/silvermic_csvs \
  --pattern "*.csv" \
  --batch-id silvermic_april_review

# Compare strategies and position plans across every replayable date in one CSV
python replay_batch.py \
  --csv /path/to/wfv_multi_session.csv \
  --all-session-dates \
  --strategy-ids silvermic_cpr_band_v3 silvermic_cpr_breakout_v1 \
  --position-plan-ids partial_t1_trail full_t1_exit \
  --batch-id strategy_plan_review

# Replay from a manifest
python replay_batch.py \
  --manifest replay_batch_manifest.example.json \
  --batch-id manifest_test
```

Batch outputs:
- per-run replay artifacts are written under `output/replay_batch/<batch_id>/runs/`
- `batch_summary.csv` ranks all runs by net P&L, average R, and drawdown
- `batch_summary.json` stores the ranked rows plus the full reports
- manifest CSV paths can be absolute or relative to the manifest file
- `--csv` can now replay one file across either a single `--session-date` or every replayable date via `--all-session-dates`
- `--strategy-ids` and `--position-plan-ids` build a strategy/lifecycle matrix without editing code
- `--date-from` and `--date-to` bound multi-session sweeps when needed
- failed runs are captured in the summary with `status=failed` instead of aborting the whole batch

### Scheduled via launchd (auto-starts at 16:50 IST weekdays)
```bash
# Install
cp ~/balas-product-os/Tools/walk_forward/com.bala.wfv.silvermic.plist \
   ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.bala.wfv.silvermic.plist

# Check status
launchctl list | grep wfv

# Trigger manually for testing
launchctl start com.bala.wfv.silvermic

# Uninstall
launchctl unload ~/Library/LaunchAgents/com.bala.wfv.silvermic.plist
```

---

## File Structure

```
walk_forward/
├── main.py              # Orchestrator — entry point
├── config.py            # All parameters and env vars
├── models.py            # Normalized InstrumentRef/Candle/Quote/DayContext
├── interfaces.py        # Provider/strategy/position/sink protocols
├── runtime.py           # Runtime composition layer
├── strategy_registry.py # Strategy registry/factory
├── position_plans.py    # Position lifecycle plan registry/factory
├── event_loop.py        # Reusable candle-processing event loop
├── replay_provider.py   # Replay market-data provider and CSV loader
├── replay.py            # Replay/backtest-compatible CLI
├── replay_batch.py      # Multi-run replay experiment CLI
├── replay_results.py    # Replay metrics + JSON/CSV artifact writers
├── upstox_provider.py   # Provider wrapper around UpstoxFeed
├── silvermic_v3_strategy.py # Strategy adapter around SignalDetector
├── silvermic_cpr_breakout_strategy.py # Breakout continuation strategy adapter
├── paper_position_manager.py # Position-manager adapter around TradeManager
├── upstox_feed.py       # Upstox REST API (instrument, OHLC, 15m candles)
├── cpr_calculator.py    # Pivot, TC, BC computation
├── supertrend.py        # ATR-based SuperTrend (matches TradingView exactly)
├── signal_detector.py   # Touch tracking + 2nd-touch entry signals
├── trade_manager.py     # Paper trade state machine (entry → T1 → trail → close)
├── notion_logger.py     # Creates and updates Notion pages
├── telegram_alerts.py   # Signal, T1, close, daily summary alerts
├── requirements.txt     # Python dependencies
└── com.bala.wfv.silvermic.plist  # launchd scheduler
```

---

## Logs

Live logs are written to: `~/Library/Logs/walk_forward/wfv_YYYY-MM-DD.log`

launchd stdout/stderr: `~/Library/Logs/walk_forward/launchd_*.log`

---

## Operational Notes

### Safe behavior
- `python main.py` is still **paper trading only**
- there is no broker order placement path in this validator
- `--dry-run` swaps in mock Notion/Telegram classes, so it is useful for feed checks only
- to validate end-to-end journaling and alerts, run normal mode
- strategy selection is config-driven through `WFV_STRATEGY_ID` or `--strategy-id`
- the default strategy is `silvermic_cpr_band_v3`
- currently registered strategies:
  - `silvermic_cpr_band_v3`
  - `silvermic_cpr_breakout_v1`
- live runner profile selection is config-driven through `WFV_PROFILE_ID` or `--profile-id`
- currently registered runner profiles:
  - `silvermic_v3_default`
  - `silvermic_breakout_research`
- position lifecycle selection is config-driven through `WFV_POSITION_PLAN_ID` or `--position-plan-id`
- the default position plan is `partial_t1_trail`
- currently registered position plans:
  - `partial_t1_trail`
  - `full_t1_exit`
  - `single_lot_t1_exit`
- bounded health checks can use `--test-duration-minutes` and `--ignore-session-window`
- multiple live paper profiles can be launched together; each runs in its own process and writes its own log file
- replay/backtest-compatible validation uses `python replay.py --self-test` or `python replay.py --csv ...`
- replay metrics and artifacts are written to `output/replay` by default
- batch replay experiments use `python replay_batch.py --self-test`, `--csv`, `--csv-dir`, or `--manifest`
- batch replay can sweep multiple session dates plus strategy/plan combinations in one run
- batch summaries are written to `output/replay_batch/<batch_id>` by default

### Smoke test status
A synthetic paper trade was executed successfully on **April 15, 2026**:
- contract resolved automatically: `SILVERMIC FUT 30 APR 26`
- instrument key: `MCX_FO|466029`
- Notion page created and updated successfully
- close details were verified in Notion

Throwaway smoke-test page:
- page id: `343d2aac-5f9d-8174-8622-e1e21727fbae`
- safe to delete manually from the Notion database

### Key fixes already applied
- `upstox_feed.py`
  - switched MCX instrument discovery to Upstox `market-quote` instrument master
  - supports epoch-millisecond expiry values from the newer master
  - uses Upstox `v3/historical-candle`
  - uses `instrument_key` for LTP lookups
- `main.py`
  - closed trades now flow into the day summary
- `trade_manager.py`
  - closed trades are tracked explicitly for later summary capture
- `config.py`
  - `NOTION_WF_DB_ID` now reads from `.env`
  - `WFV_STRATEGY_ID` can select the registered walk-forward strategy
  - `WFV_POSITION_PLAN_ID` can select the registered position lifecycle plan
- `main.py`
  - now builds components through `runtime.build_runtime()`
  - still keeps the same operator command and paper-only behavior
- new adapter layer
  - `models.py`, `interfaces.py`, `runtime.py`, `strategy_registry.py`
  - `position_plans.py`, `upstox_provider.py`, `silvermic_v3_strategy.py`, `paper_position_manager.py`
- replay layer
  - `event_loop.py`
  - `replay_provider.py`
  - `replay.py`
  - `replay_batch.py`
  - `replay_results.py`

### Tomorrow checklist
Before 16:50 IST:
- confirm `UPSTOX_ACCESS_TOKEN` is valid
- confirm `.env` contains:
  - `NOTION_API_KEY`
  - `NOTION_WF_DB_ID`
  - `TELEGRAM_BOT_TOKEN`
  - `TELEGRAM_CHAT_ID`
- confirm the Notion integration still has access to the walk-forward database
- confirm the Telegram bot chat is started

At startup:
- run `python main.py`
- confirm logs show:
  - active `SILVERMIC` contract found
  - previous-day OHLC fetched
  - warm-up candles loaded
  - CPR summary sent

Success criteria:
- day-start Telegram message arrives
- no manual `UPSTOX_SILVERMIC_*` override is required
- signal alerts create Notion pages
- closed trades update the same Notion pages
- day summary reflects real closed trades

---

## Notion Database

All paper trades go into: **Trading Journal Walk forward**
- DB ID: reads from `NOTION_WF_DB_ID` in the local `.env`
- Current database ID: supplied by the operator at runtime
- Auto-populated fields: Symbol, Direction, Entry/Exit times and prices, SL, T1, P&L, Outcome, Pre/Post notes
- Pre-trade notes include: CPR levels, CPR width, touch level, SL source
- Post-trade notes include: Exit reason, T1 P&L, trail SL level, R-multiple

---

## Troubleshooting

**"No active SILVERMIC futures found"**
The instrument master format may have changed. Check the Upstox API instruments page and update `upstox_feed.py:load_instrument()`.

**"UPSTOX_ACCESS_TOKEN" missing or expired**
The token refresh scheduler runs at 8:00 AM daily. If expired mid-session, restart after 8 AM.

**SuperTrend values look wrong**
Ensure `ST_WARMUP_BARS = 60` (default). On the first session, the SuperTrend needs ~5 bars to stabilize. SL will fall back to 0.8% during warm-up.

**Telegram not working**
Test with: `curl -X POST "https://api.telegram.org/bot<TOKEN>/sendMessage" -d "chat_id=<CHAT_ID>&text=test"`
