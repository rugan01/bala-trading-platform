# Historical Walk-Forward Roadmap

This roadmap preserves the earlier transition plan and implementation history.
Some references still point to the pre-monorepo local workspace.

# Walk-Forward Roadmap

This document describes:
- the **current stage** of the walk-forward validator
- the **target end-state**
- the **gaps** between the two
- a **safe milestone plan** to get there without destabilizing the working SILVERMIC paper-trading flow

The design principle is:
- **no drastic rewrites on the critical path**
- keep the current working validator operational
- build abstractions incrementally
- run parallel experiments before replacing the reference implementation

---

## Current Stage

Today the folder:
- [/Users/rugan/balas-product-os/Tools/walk_forward](/Users/rugan/balas-product-os/Tools/walk_forward)

contains a **working paper-trading validator centered on SILVERMIC** with:
- live paper reference strategy:
  - `SILVERMIC V3 CPR Band TC/BC Rejection`
- additional replay research variants registered in the engine

It is currently:
- **paper-only**
- **single-instrument**
- **single live instrument with multiple registered research strategies**
- registered strategies:
  - `silvermic_cpr_band_v3`
  - `silvermic_cpr_breakout_v1`
- **single provider implementation** (Upstox market data wrapped behind a provider boundary)
- registered position plans:
  - `partial_t1_trail`
  - `full_t1_exit`
  - `single_lot_t1_exit`

What already works:
- runtime composition through [runtime.py](/Users/rugan/balas-product-os/Tools/walk_forward/runtime.py)
- normalized models in [models.py](/Users/rugan/balas-product-os/Tools/walk_forward/models.py)
- protocol boundaries in [interfaces.py](/Users/rugan/balas-product-os/Tools/walk_forward/interfaces.py)
- strategy registry in [strategy_registry.py](/Users/rugan/balas-product-os/Tools/walk_forward/strategy_registry.py)
- position plan registry in [position_plans.py](/Users/rugan/balas-product-os/Tools/walk_forward/position_plans.py)
- replay event loop in [event_loop.py](/Users/rugan/balas-product-os/Tools/walk_forward/event_loop.py)
- replay provider and CLI in [replay_provider.py](/Users/rugan/balas-product-os/Tools/walk_forward/replay_provider.py) and [replay.py](/Users/rugan/balas-product-os/Tools/walk_forward/replay.py)
- replay metrics and artifact persistence in [replay_results.py](/Users/rugan/balas-product-os/Tools/walk_forward/replay_results.py)
- strategy selection via `WFV_STRATEGY_ID` or `--strategy-id`
- position plan selection via `WFV_POSITION_PLAN_ID` or `--position-plan-id`
- batch replay matrix selection via `--strategy-ids`, `--position-plan-ids`, and `--all-session-dates`
- auto-discovery of active `SILVERMIC` front-month futures
- previous-day OHLC fetch
- `15m` candle fetch
- CPR calculation
- signal generation for the V3 strategy
- paper trade lifecycle:
  - enter
  - partial at `T1`
  - trail remaining
  - force close at EOD
- alternate lifecycle experiments:
  - full exit at `T1`
  - single-lot full exit at `T1`
- Notion journaling
- Telegram alerts
- day summary capture

What is still tightly coupled:
- strategy rules are still implemented by [signal_detector.py](/Users/rugan/balas-product-os/Tools/walk_forward/signal_detector.py), wrapped by [silvermic_v3_strategy.py](/Users/rugan/balas-product-os/Tools/walk_forward/silvermic_v3_strategy.py)
- position-management rules are still implemented by [trade_manager.py](/Users/rugan/balas-product-os/Tools/walk_forward/trade_manager.py), wrapped by [paper_position_manager.py](/Users/rugan/balas-product-os/Tools/walk_forward/paper_position_manager.py)
- market data still depends on Upstox internals in [upstox_feed.py](/Users/rugan/balas-product-os/Tools/walk_forward/upstox_feed.py), wrapped by [upstox_provider.py](/Users/rugan/balas-product-os/Tools/walk_forward/upstox_provider.py)
- configuration is strategy-specific in [config.py](/Users/rugan/balas-product-os/Tools/walk_forward/config.py)

So the honest classification is:
- **specialized paper validator with first research-grade strategy/lifecycle seams**, not yet a generic strategy engine

---

## Desired End-State

The intended end-state is a system where:

1. You define a **strategy**
2. You define a **data provider / broker adapter**
3. You define a **paper or live execution mode**
4. The engine runs with minimal code changes

That end-state should support:
- strategy-agnostic operation
- broker/data-provider abstraction
- paper-first execution
- easy promotion from:
  - research
  - to replay/backtest
  - to paper trade
  - to controlled live

The desired architecture is:

- `Strategy`
  - produces signals from market state
- `MarketDataProvider`
  - resolves instruments
  - fetches historical candles
  - fetches quotes/LTP
- `ExecutionProvider`
  - paper or live trade actions
- `PositionManager`
  - lifecycle rules after entry
- `JournalSink`
  - Notion or other journals
- `AlertSink`
  - Telegram or other notifications
- `Runner / Orchestrator`
  - wires all components together

---

## Design Targets

### 1. Strategy Agnostic
The engine should not assume:
- CPR
- 2-touch rejection logic
- SuperTrend-only exits
- 2 lots
- a single `T1` target

Instead, strategies should define:
- signal inputs needed
- entry criteria
- initial stop logic
- targets
- any strategy-specific metadata

### 2. Provider Agnostic
The core engine should not depend directly on Upstox semantics like:
- instrument-master URL shapes
- quote payload shapes
- historical endpoint conventions

Instead, providers should return normalized data objects:
- instrument
- candle
- quote
- session info

### 3. Execution Agnostic
The same strategy should be able to run with:
- paper execution
- later broker-backed execution

Tomorrow's engine remains paper-only, but the design should prepare for:
- `PaperExecutionProvider`
- `UpstoxExecutionProvider`
- later other broker implementations

### 4. Risk / Position Management as a Separate Layer
The lifecycle after entry must be decoupled from signal generation.

Examples of different position managers:
- partial at `T1`, trail rest
- fixed target + full exit
- scale-in pyramid
- options spread management
- forced time exit only

This is important because “change the strategy” is not just “change the entry rule.”

---

## Constraints For Safe Evolution

To avoid breaking the working validator:

1. The current SILVERMIC V3 path remains the **reference implementation**
2. New abstractions should be built in parallel, not by rewriting everything at once
3. Each milestone must preserve the ability to run:
   - `python main.py`
4. New architecture should initially wrap the old behavior, not replace it
5. Behavioral parity must be checked before switching defaults

Recommended rule:
- **extract, don’t rewrite**

---

## Safe Milestones

## Milestone 0 — Stabilize Current Reference Path

Status:
- mostly done

Goal:
- keep the current SILVERMIC V3 validator operational for daily paper trading

Required conditions:
- auto instrument discovery stable
- historical feeds stable
- Notion and Telegram verified
- day summaries correct

Do not:
- redesign the engine before daily paper trading is reliable

Exit criteria:
- several successful paper sessions without manual recovery

---

## Milestone 1 — Introduce Interfaces Without Behavior Change

Status:
- **implemented on 2026-04-17**
- syntax/import smoke checks passed
- current SILVERMIC behavior intended to remain unchanged

Goal:
- add minimal interface boundaries while preserving the current behavior

New abstractions:
- `MarketDataProvider`
- `Strategy`
- `PositionManager`
- `JournalSink`
- `AlertSink`

Implementation approach:
- create interface-like base classes or protocols
- wrap the existing classes:
  - `UpstoxFeed` -> `UpstoxDataProvider`
  - `SignalDetector` -> first `Strategy`
  - `TradeManager` -> first `PositionManager`
  - `NotionLogger` -> `JournalSink`
  - `TelegramAlerter` -> `AlertSink`

Important:
- keep existing concrete files working
- do not change trade behavior yet

Exit criteria:
- `main.py` still runs the same way
- but components can be instantiated via interface boundaries

---

## Milestone 2 — Make Strategy Logic Pluggable

Status:
- **implemented as a safe research slice on 2026-04-18**
- strategy registry exists
- `silvermic_cpr_band_v3` remains the default registered strategy
- second strategy adapter added:
  - `silvermic_cpr_breakout_v1`
- `main.py` supports `--strategy-id`
- replay validation covers both strategy ids

Goal:
- support more than one strategy without editing the engine core

Add:
- strategy registry / factory
- strategy config block

The first extracted strategy becomes:
- `SilvermicCprBandV3Strategy`

Parallel experiment examples:
- another CPR variation
- a breakout trend strategy
- a mean-reversion variant

Do not:
- change the runner to assume one strategy class name forever

Exit criteria:
- strategy selected from config
- current V3 behavior remains unchanged

Validation snapshot:
- `replay_batch.py --list-strategies` shows both strategy ids
- breakout strategy replay smoke test completed successfully

---

## Milestone 3 — Make Position Management Pluggable

Status:
- **implemented as a safe research slice on 2026-04-18**
- `partial_t1_trail` is the default registered position plan
- additional plans added:
  - `full_t1_exit`
  - `single_lot_t1_exit`
- `TradeManager` uses the selected plan for lots, T1 exit lots, lot size, fees, and trailing behavior
- current SILVERMIC V3 behavior is preserved
- replay validation covers multiple position-plan ids

Goal:
- separate signal generation from trade lifecycle

Extract from current trade manager:
- partial exit logic
- trail logic
- EOD close behavior
- lot split assumptions

Introduce:
- `PositionPlan`
- `PositionManager`

Examples:
- `PartialT1TrailPositionManager`
- `SingleTargetFullExitPositionManager`
- `TimeExitOnlyPositionManager`

Exit criteria:
- SILVERMIC V3 uses an extracted position manager but behaves the same

Validation snapshot:
- `replay_batch.py --list-position-plans` shows all registered plan ids
- strategy/plan matrix smoke test completed successfully

---

## Milestone 4 — Normalize Market Data Models

Goal:
- hide provider-specific payloads behind normalized models

Introduce normalized models:
- `Instrument`
- `Quote`
- `Candle`
- `SessionInfo`

Provider responsibility:
- Upstox-specific parsing happens only inside the Upstox adapter

This prevents:
- strategy code from depending on broker field names
- historical/quote URL quirks leaking into strategy logic

Exit criteria:
- strategies receive normalized candles/quotes only

---

## Milestone 5 — Add Replay / Backtest-Compatible Runner

Status:
- **implemented as a first safe slice on 2026-04-17**
- `CandleEventLoop` processes normalized candles with the selected strategy and position plan
- `ReplayDataProvider` can run synthetic or CSV historical candles without live Upstox APIs
- `replay.py --self-test` validates signal -> entry -> T1 -> close behavior
- replay artifacts now include JSON summary + trades CSV
- `replay_batch.py` can run multiple replay experiments from a smoke test, CSV directory, or manifest
- `replay_batch.py` can also run one CSV across all replayable session dates with strategy/plan matrices
- batch artifacts now include ranked `batch_summary.csv` and `batch_summary.json`
- next improvement is parity reports against live paper sessions plus standardized experiment manifests

Goal:
- run the exact same strategy code in:
  - historical replay
  - paper trading
  - later live execution

Add:
- `ReplayDataProvider`
- reusable candle event loop

Why this matters:
- reduces drift between backtest logic and live paper logic
- gives safer validation before live deployment

Exit criteria:
- same strategy class works in both replay and live paper mode

---

## Milestone 6 — Multi-Instrument / Multi-Strategy Support

Status:
- **implemented as a first live-orchestration slice on 2026-04-27**
- `RunnerProfile` model added
- profile registry added
- `main.py` supports `--profile-id` and `--list-profiles`
- multiple selected profiles are launched as isolated child processes
- per-profile log files are written under `~/Library/Logs/walk_forward`
- live paper defaults still point to one `SILVERMIC` profile
- next improvement is broadening real instrument coverage beyond the current default/research profiles

Goal:
- support more than one configured runner cleanly

Examples:
- `SILVERMIC V3`
- `CRUDEOILM breakout`
- `NIFTY futures trend-follow`

Needed changes:
- instance-safe logging
- separate run state per strategy/instrument
- config-driven runner definitions

Exit criteria:
- multiple paper validators can run independently without shared-state collisions

Validation snapshot:
- `python main.py --list-profiles` shows registered profiles
- `python main.py --help` shows the new profile CLI options

---

## Milestone 7 — Execution Provider Abstraction

Goal:
- prepare for eventual live trading without changing strategy logic

Introduce:
- `PaperExecutionProvider`
- later `LiveBrokerExecutionProvider`

Important safety rule:
- live execution must remain opt-in and isolated
- paper remains default

Exit criteria:
- paper/live mode toggled at configuration level
- strategy code unchanged

---

## Milestone 8 — Controlled Live Deployment

Goal:
- use the same engine for live orders only after replay + paper are stable

Conditions before enabling:
- replay parity validated
- paper sessions stable
- explicit live safeguards:
  - kill switch
  - max loss / max trades
  - order audit logs
  - broker acknowledgement tracking

Exit criteria:
- first live deployment with one strategy and tight controls

---

## Suggested Folder Evolution

Current files should stay intact initially.

Possible future shape:

```text
walk_forward/
├── main.py                         # keep current entry point initially
├── config.py
├── strategies/
│   ├── base.py
│   └── silvermic_cpr_v3.py
├── providers/
│   ├── market_data/
│   │   ├── base.py
│   │   └── upstox.py
│   └── execution/
│       ├── base.py
│       ├── paper.py
│       └── upstox_live.py
├── position_managers/
│   ├── base.py
│   └── partial_t1_trail.py
├── sinks/
│   ├── journal/
│   │   └── notion.py
│   └── alerts/
│       └── telegram.py
├── models/
│   ├── market.py
│   ├── signals.py
│   └── trades.py
└── legacy/
    └── ... optional if old code is retired later
```

Important:
- do this gradually
- do not move everything at once before parity checks

---

## Recommended Build Strategy

Use two tracks:

### Track A — Production-Safe
- keep current SILVERMIC runner working
- only apply small bug fixes
- use it for daily paper trading

### Track B — Refactor / Experiments
- build new abstractions in parallel
- wrap existing behavior first
- validate parity using synthetic tests and replay

Switch only when:
- Track B produces the same behavior as Track A for the reference strategy

---

## What Not To Do

Avoid:
- rewriting the whole engine in one pass
- mixing live execution into the refactor early
- changing strategy behavior while introducing abstractions
- making the runner multi-strategy before core interfaces are stable

These are the fastest ways to lose the working baseline.

---

## Next Best Step

The safest next milestone after tomorrow’s paper session is:

1. keep the current runtime unchanged
2. introduce a small set of base interfaces
3. wrap:
   - `UpstoxFeed`
   - `SignalDetector`
   - `TradeManager`
4. prove no behavior change

Detailed Milestone 1 interface note:
- [MILESTONE_1_DESIGN.md](/Users/rugan/balas-product-os/Tools/walk_forward/MILESTONE_1_DESIGN.md)

That gets the architecture moving without risking the current operational path.
