# Institutional Trading Platform Roadmap

**Created**: April 30, 2026  
**Purpose**: Define the target architecture and milestone plan for evolving the current scripts, analyzers, journaling, and walk-forward validators into a broker-agnostic trading platform that can support:
- research and backtesting
- forward testing / paper trading
- live execution
- trade journaling and reconciliation
- risk controls and kill switch
- real-time monitoring and dashboarding

---

## 1. Where We Are Today

The current system already has valuable working pieces:

### Existing strengths
- `apps/journaling/trade_journaling.py`
  - same-day Upstox trade pull
  - Notion journal upsert flow
  - stable `Journal Key` dedupe
- `apps/journaling/broker_trade_backfill.py`
  - broker XLSX reconciliation
  - historical time repair
  - missed-day recovery
- `apps/walk-forward/`
  - paper-only strategy validation engine
  - replay and batch replay
  - normalized interfaces beginning to emerge
- analyzers and premarket tooling
  - MCX and F&O scanning
  - morning brief generation
  - strategy-specific market analysis

### Current limits
- logic is spread across scripts rather than one cohesive platform
- strategy execution, journaling, reference data, and monitoring are not yet driven by one canonical data model
- Upstox-specific logic still leaks into too many places
- there is no central instrument/reference archive yet
- risk controls and kill switch are not platform-level
- no unified dashboard for positions, P&L, orders, and system health

So the current stack is **useful and operational**, but it is still a **toolbox**, not yet an **institutional-style platform**.

---

## 2. Target End-State

The target system should behave like this:

1. A strategy produces normalized signals.
2. A portfolio/risk layer decides whether they are allowed.
3. An execution layer routes paper or live orders to one or more brokers.
4. A monitoring layer tracks positions, fills, P&L, risk, and system health in real time.
5. A journaling/reconciliation layer records the truth without depending on one broker or one UI.
6. Backtest, forward test, and live trade all use the **same normalized models**.

### Design principles
- broker agnostic
- market-data-provider agnostic
- append-only event capture for auditability
- paper-first promotion path
- risk engine separate from strategy logic
- one normalized domain model for orders, fills, instruments, positions, and strategy runs
- no big-bang rewrite; evolve safely from the current codebase

---

## 3. Architectural Shape

The platform should be split into the following layers.

### A. Core Domain
Pure normalized models, no broker-specific code.

Examples:
- `Account`
- `Broker`
- `Instrument`
- `InstrumentAlias`
- `OrderIntent`
- `Order`
- `Fill`
- `Position`
- `PortfolioSnapshot`
- `Signal`
- `StrategyRun`
- `RiskLimit`
- `KillSwitchEvent`

### B. Application Services
Use cases that orchestrate domain actions.

Examples:
- trade ingestion
- order reconciliation
- journal generation
- risk evaluation
- execution workflow
- forward-test runner
- strategy replay runner
- daily close / expiry handling

### C. Adapter Layer
External integrations that translate to/from the domain model.

Adapters needed:
- `MarketDataProvider`
- `ExecutionBroker`
- `JournalSink`
- `AlertSink`
- `DashboardAPI`

Initial concrete adapters:
- `UpstoxMarketDataAdapter`
- `UpstoxExecutionAdapter`
- `NotionJournalAdapter`
- `TelegramAlertAdapter`

### D. Storage Layer
Canonical local archive and projections.

Stores needed:
- control/transaction store
- market data store
- analytics/backtest store
- projection tables for dashboard/journaling/risk

### E. Runtime Layer
Long-running processes.

Processes needed:
- archive sync worker
- strategy runner
- execution worker
- risk monitor
- position monitor
- dashboard backend

---

## 4. Recommended Repo / Folder Direction

Do **not** keep growing this only as loose standalone scripts.

Recommended project root:
- `/path/to/bala-trading-platform/`

Suggested layout:

```text
.
  README.md
  docs/
  apps/
  packages/
  data/
    archive/
    raw/
    reports/
```

Current app groups can remain operational and gradually become wrappers around more formal platform services.

---

## 5. Core Platform Modules

### 5.1 Instrument Reference Service
This becomes the foundation for everything else.

Responsibilities:
- maintain canonical instruments across brokers and exchanges
- map aliases and symbol-format variants
- store lot size, tick size, expiry, segment, option metadata
- version instrument snapshots by date

This solves recurring problems like:
- symbol parsing drift
- monthly vs weekly expiry ambiguity
- broker symbol mismatch
- missing lot size during journaling or risk

### 5.2 Trade Archive Service
This is the immediate next practical step.

Responsibilities:
- archive raw Upstox trade/order payloads on the same day
- store normalized fills and orders
- store broker-file fallback imports
- preserve `trade_id -> instrument_token -> instrument reference snapshot`
- become the canonical source for journaling, reconciliation, and later risk/monitoring

### 5.3 Strategy Runtime
Unified runner for:
- backtest
- replay
- paper trading
- live execution

The strategy should only emit:
- signal / intent
- suggested stop / target / metadata

It should **not** directly talk to brokers or journals.

### 5.4 Risk Engine
Separate from strategy logic.

Responsibilities:
- account-level max loss
- strategy-level max loss
- per-trade risk
- max concurrent positions
- max open exposure by segment
- slippage/rejection guardrails
- kill switch trigger evaluation

### 5.5 Execution Engine
Responsibilities:
- translate intents to broker orders
- manage order state machine
- track acknowledgements, rejects, partial fills, cancellations
- emit normalized fill events

### 5.6 Monitoring / Dashboard
Responsibilities:
- view live positions across accounts and brokers
- per-strategy P&L
- order status and errors
- kill switch status
- heartbeat / data freshness
- realized vs unrealized P&L

---

## 6. Storage Recommendation

### Phase 1 Storage
Keep it simple and local first.

Use:
- **SQLite** for transactional/control/archive data
- **Parquet** files for heavier historical candle / quote datasets
- optional **DuckDB** for analytics queries later

Why:
- low operational burden
- good for single-user local workflows
- easy backup/versioning
- enough for current scale

### Phase 2 Storage
When the platform becomes always-on or multi-service:
- migrate the control plane to **Postgres**
- optionally use **TimescaleDB** or **ClickHouse** for heavy market timeseries

### Immediate archive location

Recommended local archive root:
- `/path/to/bala-trading-platform/data/archive/`

Suggested files:
- `/path/to/bala-trading-platform/data/archive/platform.sqlite3`
- `/path/to/bala-trading-platform/data/archive/raw/YYYY-MM-DD/`

---

## 7. Archive Schema For Phase 1

The archive should store both raw payloads and normalized records.

### Core tables

#### `accounts`
- `account_id`
- `account_code` (`BALA`, `NIMMY`, etc.)
- `broker_name`
- `base_currency`
- `is_active`

#### `instruments`
- `instrument_id`
- `broker_name`
- `instrument_token`
- `trading_symbol`
- `exchange`
- `segment`
- `base_symbol`
- `instrument_type`
- `option_type`
- `strike`
- `expiry_date`
- `lot_size`
- `tick_size`
- `snapshot_date`

#### `instrument_aliases`
- `alias_id`
- `instrument_id`
- `source`
- `alias_value`

#### `trade_orders`
- `order_pk`
- `broker_name`
- `account_code`
- `order_id`
- `trade_date`
- `trading_symbol`
- `instrument_token`
- `transaction_type`
- `product`
- `exchange`
- `raw_payload_json`

#### `trade_fills`
- `fill_pk`
- `broker_name`
- `account_code`
- `trade_id`
- `order_id`
- `trade_date`
- `exchange_timestamp`
- `order_timestamp`
- `trading_symbol`
- `instrument_token`
- `normalized_instrument_id`
- `transaction_type`
- `quantity`
- `price`
- `fees`
- `source_type` (`upstox_api`, `broker_xlsx`)
- `raw_payload_json`

#### `journal_links`
- `journal_key`
- `notion_page_id`
- `account_code`
- `trade_date`
- `status`

#### `strategy_runs`
- `run_id`
- `strategy_id`
- `mode` (`backtest`, `replay`, `paper`, `live`)
- `account_code`
- `broker_name`
- `started_at`
- `ended_at`
- `status`

#### `position_snapshots`
- `snapshot_id`
- `captured_at`
- `account_code`
- `broker_name`
- `instrument_id`
- `net_quantity`
- `avg_price`
- `mtm`
- `realized_pnl`
- `unrealized_pnl`

#### `risk_events`
- `event_id`
- `captured_at`
- `risk_scope`
- `severity`
- `message`
- `strategy_id`
- `account_code`

#### `kill_switch_events`
- `event_id`
- `captured_at`
- `scope`
- `trigger_type`
- `state`
- `message`

---

## 8. How The Local Archive Fits Immediately

The archive should be implemented **before** we try to make the whole system live-execution grade.

### Why it should come first
- fixes same-day journaling fragility
- preserves exact trade metadata before Upstox historical limitations kick in
- gives one canonical source for future reconciliation
- reduces dependence on re-parsing symbols from scratch
- makes risk and dashboard layers possible later

### Immediate behavior to add

On every same-day journaling run:
1. call Upstox same-day trade/order endpoints
2. save raw JSON to disk
3. normalize and upsert instruments
4. normalize and upsert fills/orders into the archive
5. only then generate or update the Notion journal

On broker XLSX fallback:
1. parse broker export
2. normalize and upsert fill records into the same archive
3. reconcile missing fields
4. repair or create journal rows from archive-backed truth

That means:
- `trade_journaling.py` eventually becomes a **consumer** of the archive, not the primary truth source
- `broker_trade_backfill.py` becomes an archive repair/import tool, not a standalone special-case script

---

## 9. Broker-Agnostic Contract Boundaries

These should be formal interfaces early.

### `MarketDataProvider`
Methods:
- `list_instruments()`
- `resolve_instrument()`
- `get_quote()`
- `get_candles()`
- `stream_quotes()`

### `ExecutionBroker`
Methods:
- `place_order()`
- `modify_order()`
- `cancel_order()`
- `get_order_status()`
- `get_positions()`
- `get_fills_for_day()`

### `JournalSink`
Methods:
- `upsert_trade_row()`
- `close_trade_row()`
- `link_archive_record()`

### `AlertSink`
Methods:
- `send_info()`
- `send_warning()`
- `send_critical()`

---

## 10. Risk Engine And Kill Switch Design

The kill switch must be platform-owned, not trader-memory-owned.

### Strategy-level controls
- max trades per day
- max loss per day
- max loss per instrument
- cooldown after stop-out cluster

### Account-level controls
- max realized drawdown
- max open risk
- max gross exposure
- broker disconnect / stale quote kill switch

### Platform-level controls
- data heartbeat failure
- order reject burst
- fill mismatch / reconciliation anomaly
- journal/archive write failure severity

### Kill switch actions
- stop new orders
- cancel working orders
- flatten selected strategies
- flatten account
- send Telegram + dashboard critical alert

---

## 11. Monitoring And Dashboard Scope

The dashboard should answer these questions instantly:
- what is live right now?
- what is open by broker/account/strategy?
- what is today’s realized and unrealized P&L?
- which strategies are in paper vs live mode?
- is any kill switch armed?
- are data feeds healthy?
- which orders are rejected/stuck/partially filled?

### Suggested initial dashboard views
- positions by account
- positions by strategy
- live order blotter
- fills feed
- daily P&L
- risk status
- archive/journal sync status

Phase 1 can be a lightweight local web UI; do not overbuild this first.

---

## 12. Safe Roadmap

This should be built as **small milestones**, not one massive refactor.

### Milestone 0 — Freeze Current Truth Sources
Goal:
- stabilize current scripts and document known flows

Deliverables:
- current journaling repaired
- broker backfill repaired
- walk-forward paper engine remains operational

Status:
- mostly done

### Milestone 1 — Build Local Archive First
Goal:
- capture same-day Upstox raw payloads and normalized fills/orders/instruments locally

Deliverables:
- `platform.sqlite3`
- raw JSON snapshot storage
- archive writer module
- archive lookup by `trade_id`, `order_id`, `instrument_token`

This is the **next recommended build step**.

### Milestone 2 — Make Journaling Archive-Backed
Goal:
- journaling reads from archive first, API second

Deliverables:
- `trade_journaling.py` uses archive-backed normalized records
- `broker_trade_backfill.py` writes into archive and reconciles from archive
- Notion becomes projection/output, not truth source

### Milestone 3 — Central Instrument Reference Service
Goal:
- one consistent resolution path for symbol, expiry, strike, lot size, instrument type

Deliverables:
- canonical instrument table
- alias mapping rules
- expiry/version snapshot handling

### Milestone 4 — Shared Domain Models + Event Bus
Goal:
- unify journaling, walk-forward, and analyzers on one model layer

Deliverables:
- normalized models package
- event types for signals, intents, fills, positions, risk events

### Milestone 5 — Unified Paper Execution Engine
Goal:
- all forward tests use one execution/risk lifecycle

Deliverables:
- paper broker adapter
- strategy runner framework
- reusable position monitor

### Milestone 6 — Risk Engine + Kill Switch
Goal:
- enforce platform-level risk

Deliverables:
- risk rule config
- live risk evaluator
- kill switch actions and audit trail

### Milestone 7 — Monitoring API + Dashboard
Goal:
- real-time observability

Deliverables:
- read API over archive/projections
- UI for positions, P&L, risk, health

### Milestone 8 — Live Upstox Execution Adapter
Goal:
- controlled live execution through the same platform

Deliverables:
- Upstox execution adapter
- order/fill reconciliation loop
- paper/live switch at runtime config only

### Milestone 9 — Multi-Broker / Multi-Strategy Portfolio
Goal:
- truly broker-agnostic, multi-account, multi-strategy deployment

Deliverables:
- second broker adapter
- account-level portfolio aggregation
- broker comparison / routing capability

---

## 13. What Not To Do

Avoid these mistakes:
- do not rewrite all current scripts at once
- do not start with live order execution
- do not make Notion the system of record
- do not make the dashboard before the archive exists
- do not overfit architecture to Upstox naming quirks
- do not bind strategy code directly to broker payloads

---

## 14. Recommended Immediate Next Actions

### Next build slice
Build **Milestone 1** only:
- local archive package
- SQLite schema
- raw JSON snapshot writer
- same-day Upstox ingestion command
- archive lookup CLI

### Concrete implementation suggestion
Use the monorepo root:
- `/path/to/bala-trading-platform/`

Phase-1 files:

```text
.
  README.md
  docs/
  packages/trading_platform/src/trading_platform/
    archive/
      models.py
      schema.py
      store.py
      upstox_ingest.py
    cli/
      archive_cli.py
  data/
    archive/
```

Then:
1. ingest same-day Upstox trade payloads for `BALA` and `NIMMY`
2. store raw payload + normalized fills
3. use that archive to power journaling

That gives the next layer something reliable to stand on.

---

## 15. Honest Recommendation

The right path is:
- **do not jump straight to live trading infrastructure**
- **first create the archive and canonical reference layer**
- then unify journaling and walk-forward around that truth source
- then add risk and kill switch
- only then move toward true broker execution

The platform should become:
- **archive first**
- **execution second**
- **dashboard third**

That sequence gives the best odds of building something institutional without breaking what already works.
