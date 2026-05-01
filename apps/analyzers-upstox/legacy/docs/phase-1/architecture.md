# Architecture

## Core Principle

The system must treat the broker as an external service behind a stable internal contract. The trading platform should depend on interfaces such as `BrokerAdapter`, `MarketDataAdapter`, and `ExecutionAdapter`, not on Upstox-specific request or response formats.

If a broker changes later, only the adapter implementation and broker config should need to change. Strategy code, backtesting, journaling, risk rules, storage, and monitoring should remain unchanged.

## High-Level Architecture

```text
+------------------------+
|     Config Layer       |
| env + YAML/TOML/JSON   |
+-----------+------------+
            |
            v
+------------------------+      +------------------------+
|    Orchestrator/API    |----->|   Monitoring/Alerts    |
+-----------+------------+      +------------------------+
            |
            v
+------------------------+
|     Domain Services    |
| strategy, risk,        |
| portfolio, journal     |
+-----+-----------+------+
      |           |
      v           v
+-----------+  +----------------+
| Backtests  |  | Live Execution |
+-----+------+  +-------+--------+
      |                 |
      v                 v
+---------------------------------------+
|           Adapter Interfaces           |
| market data, broker, execution, auth  |
+----------------+----------------------+
                 |
                 v
+---------------------------------------+
|    Upstox Adapter (Phase 1 broker)    |
+---------------------------------------+
                 |
                 v
+---------------------------------------+
|                Storage                 |
| postgres + object storage/logs         |
+---------------------------------------+
```

## Logical Modules

### 1. Config Layer

Responsibilities:
- Select active broker adapter
- Define market segment availability
- Configure fees, taxes, slippage, risk limits, and trading calendars
- Keep environment-specific settings outside business logic

Recommended config groups:
- `app`
- `broker`
- `market_data`
- `execution`
- `risk`
- `backtest`
- `journal`
- `alerts`

Example shape:

```yaml
app:
  env: dev
  timezone: Asia/Kolkata

broker:
  provider: upstox
  mode: paper
  adapters:
    upstox:
      api_key_env: UPSTOX_API_KEY
      api_secret_env: UPSTOX_API_SECRET
      redirect_uri: http://localhost:3000/callback
      base_url: https://api.upstox.com
    zerodha:
      api_key_env: ZERODHA_API_KEY
      api_secret_env: ZERODHA_API_SECRET

market_data:
  provider: broker
  timeframe: 1d
  symbols_universe: nse_large_midcap

execution:
  order_type_default: market
  product_type: delivery
  reconcile_interval_seconds: 30

risk:
  max_risk_per_trade_pct: 0.5
  max_open_positions: 10
  max_daily_loss_pct: 1.5
  kill_switch_enabled: true

backtest:
  slippage_bps: 10
  walk_forward_train_months: 36
  walk_forward_test_months: 6

journal:
  review_template: phase1_swing
```

Decision:
- Keep broker-specific secrets and endpoint settings in config
- Keep broker capability flags in adapter code, not in strategy logic

### 2. Instrument Master Service

Responsibilities:
- Maintain canonical internal instrument IDs
- Map broker-specific instrument identifiers to internal identifiers
- Track exchange, segment, tick size, lot size, expiry, and status

This service is essential for broker portability. The rest of the system should work with internal IDs such as `instrument_id`, not raw broker instrument keys.

### 3. Market Data Service

Responsibilities:
- Download and normalize historical candles
- Subscribe to live feeds when needed
- Apply instrument metadata and trading calendar rules
- Store candles in a broker-neutral schema

Broker-neutral output contract:
- OHLCV candle
- timestamp
- exchange
- symbol
- timeframe
- corporate-action adjusted flag
- source name

### 4. Strategy Service

Responsibilities:
- Generate signals from normalized market data
- Remain pure and deterministic for research reproducibility
- Output intent, not broker orders

Strategy output contract:
- `signal_id`
- `instrument_id`
- `strategy_id`
- `signal_timestamp`
- `side`
- `confidence`
- `entry_logic`
- `exit_logic`
- `position_sizing_hint`

### 5. Risk Engine

Responsibilities:
- Accept strategy intent
- Enforce exposure, sizing, concentration, and kill-switch rules
- Produce approved trade intents or block reasons

This module must be shared by backtesting and live trading so simulation and deployment use the same guardrails.

### 6. Portfolio Engine

Responsibilities:
- Maintain positions, realized PnL, unrealized PnL, cash, and exposure
- Reconcile internal state with broker state
- Provide a canonical account state to risk and journal modules

### 7. Execution Engine

Responsibilities:
- Convert approved intents into broker orders
- Manage retries, idempotency, duplicate protection, and reconciliation
- Track order lifecycle independent of broker naming

Execution flow:
1. Receive approved trade intent
2. Enrich with execution policy
3. Generate internal order request
4. Send through active broker adapter
5. Persist raw response and normalized order state
6. Reconcile fills and update positions

### 8. Journal Intelligence Service

Responsibilities:
- Store planned trade thesis and actual trade outcome
- Detect rule violations and discretionary overrides
- Produce review feedback from data first
- Optionally layer LLM-generated commentary on top of statistics

Guiding rule:
- Statistical review is the source of truth
- LLM commentary is an assistant, not the judge

### 9. Backtesting Engine

Responsibilities:
- Replay normalized historical data
- Use the same strategy, risk, and portfolio logic as live mode
- Model slippage, fees, taxes, liquidity, and futures roll assumptions
- Support walk-forward testing

### 10. Monitoring and Operations

Responsibilities:
- Health checks
- Run logs
- Order exceptions
- Data freshness alerts
- Reconciliation mismatch alerts
- Daily summary reports

## Broker-Agnostic Boundary

## Internal Contracts

The platform should define interfaces like:

```text
BrokerAdapter
  authenticate()
  place_order(order_request)
  cancel_order(order_id)
  get_order(order_id)
  list_orders(from_ts, to_ts)
  list_positions()
  list_holdings()
  get_fills(from_ts, to_ts)

MarketDataAdapter
  fetch_historical_candles(instrument_id, timeframe, start, end)
  stream_quotes(instrument_ids)
  stream_order_updates()

InstrumentAdapter
  sync_instruments()
  map_external_to_internal()
```

Rules:
- Core modules can only import interface contracts, never adapter-specific code
- Adapters must normalize all broker payloads before returning them
- Broker raw payloads should still be stored for audit/debugging

## Upstox in Phase 1

Upstox should be implemented as:
- `UpstoxBrokerAdapter`
- `UpstoxMarketDataAdapter`
- `UpstoxInstrumentAdapter`

Upstox-specific concerns should live only inside these adapters:
- auth flow
- instrument key format
- rate limits
- endpoint paths
- websocket payload parsing
- order state mappings

## Suggested Runtime Components

For Phase 1, a simple deployment layout is enough:
- `scheduler`: runs daily scans, EOD processing, backfills, and reports
- `api`: manual control endpoints, reporting endpoints, and health endpoints
- `worker`: execution, reconciliation, journal processing
- `db`: postgres
- `cache` optional: redis for queues, locks, and short-lived state

## Decision Summary

- Use a modular monolith first, not microservices
- Keep one relational database as the system of record
- Normalize all external data into internal schemas
- Reuse the same strategy/risk/portfolio logic in backtests and live mode
- Isolate all broker details behind adapters selected through config
