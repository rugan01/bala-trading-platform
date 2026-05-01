# Implementation Plan

## Phase 1 Goal

Deliver a reliable platform for:
- daily-bar backtesting on Indian cash equities
- optional extension to index futures
- paper trading through Upstox
- controlled live trading after paper validation
- structured journal review from day one

## Delivery Sequence

### Milestone 1: Project Skeleton

Deliverables:
- modular monolith repository structure
- config system
- domain models
- adapter interfaces
- environment profiles for `dev`, `paper`, and `live`

Suggested folders:

```text
src/
  app/
  config/
  domain/
    instruments/
    market_data/
    strategies/
    risk/
    portfolio/
    execution/
    journal/
    backtest/
  adapters/
    broker/
      upstox/
    market_data/
      upstox/
  infra/
    db/
    logging/
    scheduler/
  api/
docs/
  phase-1/
```

Exit criteria:
- active broker is selected from config
- core code imports only interfaces, not Upstox-specific modules

### Milestone 2: Instrument Master and Data Ingestion

Deliverables:
- instrument sync pipeline
- instrument mapping layer
- daily candle ingestion and backfill
- ingestion run tracking

Key decisions:
- internal instrument IDs are mandatory
- historical candles are stored in normalized form
- broker payloads are also stored raw for audit

Exit criteria:
- can backfill at least 5 years of daily candles for the selected equity universe
- can refresh the latest daily bar incrementally

### Milestone 3: Backtesting Core

Deliverables:
- event-driven or bar-driven backtest engine
- fee/slippage/tax model
- portfolio and position accounting
- summary metrics and trade logs

Validation rules:
- no future leakage
- deterministic reruns
- same risk logic as live mode

Exit criteria:
- can run a full backtest for a sample strategy over 5 years
- outputs trades, equity curve, drawdown, CAGR, win rate, expectancy, MAE, and MFE

### Milestone 4: Walk-Forward Research Workflow

Deliverables:
- rolling train/test windows
- run registry in `backtest_runs`
- parameter versioning
- comparison reports

Recommended standard:
- train window: 36 months
- test window: 6 months
- roll forward until all data is covered

Exit criteria:
- strategy performance can be judged across multiple out-of-sample windows, not one lucky split

### Milestone 5: First Strategy

Deliverables:
- one daily swing strategy only
- signal generation with clear entry and exit rules
- signal persistence

Recommended strategy characteristics:
- liquid equities only
- trend filter plus breakout or pullback entry
- hard stop, trailing exit, and max holding period

Exit criteria:
- one strategy can produce reproducible daily signals and backtest results

### Milestone 6: Journal System

Deliverables:
- journal entry creation on trade open and close
- fields for thesis, setup, risk, rule violations, and notes
- basic post-trade analytics

System-generated checks:
- exited before rule condition
- moved stop wider
- added size outside rules
- held losers beyond allowed window
- took trades outside approved universe

Exit criteria:
- every live or paper trade has a linked journal record
- weekly review can identify repeat errors and edge concentration

### Milestone 7: Upstox Paper Execution Adapter

Deliverables:
- auth flow
- instrument lookup mapping
- normalized order placement
- order update ingestion
- fills reconciliation

Important rule:
- the adapter only translates between internal contracts and Upstox APIs

Exit criteria:
- paper orders can be submitted, tracked, and reconciled through the normalized internal order model

### Milestone 8: Risk Controls and Operations

Deliverables:
- position sizing rules
- concentration limits
- max daily loss rules
- kill switch
- alerting and health checks

Exit criteria:
- execution can be halted safely
- stale data, reconciliation mismatch, and failed orders are visible immediately

### Milestone 9: Controlled Live Rollout

Deliverables:
- live mode checklist
- reduced capital launch settings
- runbook for failures and manual intervention

Launch path:
1. backtest validation
2. walk-forward validation
3. paper trade for multiple weeks
4. small-capital live deployment
5. scale only after stable reconciliation and journal-confirmed edge

## Recommended Tech Decisions

- Backend: Python
- API framework: FastAPI
- Database: PostgreSQL
- Queue/scheduler: Celery or a lightweight scheduler first, with Redis if needed
- ORM: SQLAlchemy
- Data analysis: pandas and numpy
- Backtest reporting: custom analytics plus notebook/report export

Reasoning:
- Python has the best balance for research, backtesting, and integration work
- FastAPI is enough for operator control and internal services
- Postgres is reliable and simple for normalized trading state

## Phase 1 Research Standards

- Use adjusted equity data where appropriate
- Include charges, taxes, and slippage in every backtest
- Restrict to liquid names only at first
- Avoid options in Phase 1
- Add futures only after the cash-equity path is stable
- Never promote a strategy based on a single split or a single metric

## Exact Build Order

1. Create repo skeleton, config system, and interface contracts
2. Implement instrument master and broker-neutral IDs
3. Implement candle ingestion and storage
4. Build the backtest engine and portfolio accounting
5. Add walk-forward testing
6. Implement the first daily strategy
7. Build journal capture and review analytics
8. Implement Upstox adapters for data and execution
9. Add paper-trading orchestration
10. Add risk controls, monitoring, and reconciliation
11. Run paper trading until behavior matches expectations
12. Enable constrained live trading

## Immediate Next Build Step

The first engineering task should be:

`Create the repository skeleton and formalize the broker adapter contracts before writing any Upstox-specific integration code.`

That decision protects the architecture from vendor lock-in from the very beginning.
