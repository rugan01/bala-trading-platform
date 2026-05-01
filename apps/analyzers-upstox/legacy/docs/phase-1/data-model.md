# Data Model

## Storage Principles

- Postgres is the system of record for Phase 1
- Internal normalized tables come first
- Raw broker and market-data payloads are stored for audit and replay
- Long-term journaling must not depend on broker retention windows

## Core Tables

### `brokers`

Purpose:
- Register available broker providers and capabilities

Key columns:
- `broker_id`
- `name`
- `provider_code`
- `is_active`
- `supports_equities`
- `supports_futures`
- `supports_options`
- `supports_live_trading`
- `created_at`

### `broker_accounts`

Purpose:
- Store configured broker accounts and runtime mode

Key columns:
- `broker_account_id`
- `broker_id`
- `account_label`
- `client_code`
- `mode` (`paper`, `live`)
- `status`
- `last_sync_at`
- `created_at`

### `instruments`

Purpose:
- Canonical instrument master

Key columns:
- `instrument_id`
- `exchange`
- `segment`
- `symbol`
- `trading_symbol`
- `instrument_type`
- `base_asset`
- `quote_asset`
- `tick_size`
- `lot_size`
- `expiry_date`
- `strike_price`
- `option_type`
- `status`
- `first_seen_at`
- `last_seen_at`

### `instrument_mappings`

Purpose:
- Map internal instruments to broker-specific identifiers

Key columns:
- `instrument_mapping_id`
- `instrument_id`
- `broker_id`
- `external_instrument_key`
- `external_symbol`
- `valid_from`
- `valid_to`
- `is_current`

### `trading_calendars`

Purpose:
- Store exchange trading sessions and holidays

Key columns:
- `calendar_id`
- `exchange`
- `trade_date`
- `is_open`
- `session_open_at`
- `session_close_at`

### `corporate_actions`

Purpose:
- Track splits, bonuses, dividends, mergers, and symbol changes

Key columns:
- `corporate_action_id`
- `instrument_id`
- `action_type`
- `ex_date`
- `ratio_old`
- `ratio_new`
- `cash_amount`
- `source`

### `candles`

Purpose:
- Normalized OHLCV history

Key columns:
- `candle_id`
- `instrument_id`
- `timeframe`
- `candle_start`
- `open`
- `high`
- `low`
- `close`
- `volume`
- `open_interest` nullable
- `is_adjusted`
- `source`
- `ingested_at`

Indexes:
- unique on `instrument_id`, `timeframe`, `candle_start`

### `data_ingestion_runs`

Purpose:
- Track backfills and incremental sync health

Key columns:
- `ingestion_run_id`
- `source_type`
- `source_name`
- `started_at`
- `completed_at`
- `status`
- `records_written`
- `error_summary`

### `strategies`

Purpose:
- Register strategy definitions

Key columns:
- `strategy_id`
- `name`
- `version`
- `timeframe`
- `status`
- `description`
- `created_at`

### `strategy_parameters`

Purpose:
- Store versioned strategy settings used in research and deployment

Key columns:
- `strategy_parameter_id`
- `strategy_id`
- `version`
- `parameter_blob`
- `is_active`
- `created_at`

### `signals`

Purpose:
- Persist generated signals before risk approval

Key columns:
- `signal_id`
- `strategy_id`
- `instrument_id`
- `signal_timestamp`
- `side`
- `signal_strength`
- `entry_reason`
- `exit_reason`
- `recommended_stop`
- `recommended_target`
- `position_sizing_hint`
- `run_context`

### `trade_intents`

Purpose:
- Record risk-reviewed intents that may become orders

Key columns:
- `trade_intent_id`
- `signal_id`
- `broker_account_id`
- `intent_timestamp`
- `side`
- `quantity`
- `risk_state` (`approved`, `blocked`, `reduced`)
- `risk_reason`
- `execution_policy`

### `orders`

Purpose:
- Internal normalized order lifecycle

Key columns:
- `order_id`
- `trade_intent_id`
- `broker_account_id`
- `instrument_id`
- `client_order_id`
- `broker_order_id`
- `side`
- `order_type`
- `product_type`
- `quantity`
- `price`
- `trigger_price`
- `status`
- `placed_at`
- `updated_at`

### `fills`

Purpose:
- Execution fills from broker reconciled into internal format

Key columns:
- `fill_id`
- `order_id`
- `broker_trade_id`
- `instrument_id`
- `fill_timestamp`
- `quantity`
- `price`
- `fees`
- `taxes`
- `net_amount`

### `positions`

Purpose:
- Canonical portfolio state by instrument

Key columns:
- `position_id`
- `broker_account_id`
- `instrument_id`
- `quantity`
- `average_price`
- `realized_pnl`
- `unrealized_pnl`
- `market_value`
- `last_mark_price`
- `as_of_timestamp`

### `portfolio_snapshots`

Purpose:
- End-of-day and intraday account state snapshots

Key columns:
- `portfolio_snapshot_id`
- `broker_account_id`
- `snapshot_timestamp`
- `cash_balance`
- `equity_value`
- `margin_used`
- `gross_exposure`
- `net_exposure`
- `day_pnl`
- `total_pnl`

### `backtest_runs`

Purpose:
- Track each research experiment

Key columns:
- `backtest_run_id`
- `strategy_id`
- `parameter_version`
- `train_start`
- `train_end`
- `test_start`
- `test_end`
- `walk_forward_window`
- `slippage_model`
- `fee_model`
- `status`
- `summary_metrics`
- `created_at`

### `backtest_trades`

Purpose:
- Store simulated trades for analysis and comparison with live behavior

Key columns:
- `backtest_trade_id`
- `backtest_run_id`
- `instrument_id`
- `entry_timestamp`
- `exit_timestamp`
- `entry_price`
- `exit_price`
- `quantity`
- `gross_pnl`
- `net_pnl`
- `mae`
- `mfe`
- `holding_period_days`

### `journal_entries`

Purpose:
- Human and system notes on trades

Key columns:
- `journal_entry_id`
- `trade_ref_type` (`live`, `backtest`, `manual`)
- `trade_ref_id`
- `entry_timestamp`
- `setup_type`
- `intended_holding_period`
- `actual_holding_period`
- `planned_risk`
- `actual_risk`
- `emotional_state`
- `notes`
- `rule_violation_flags`

### `trade_reviews`

Purpose:
- Generated post-trade and periodic review output

Key columns:
- `trade_review_id`
- `journal_entry_id`
- `review_type` (`post_trade`, `weekly`, `monthly`)
- `metrics_blob`
- `findings`
- `suggestions`
- `generated_by`
- `generated_at`

### `raw_events`

Purpose:
- Audit trail of broker and market-data payloads

Key columns:
- `raw_event_id`
- `source_type`
- `source_name`
- `event_type`
- `external_ref`
- `payload`
- `received_at`

## Relationships

- `brokers` -> `broker_accounts`
- `instruments` -> `instrument_mappings`
- `instruments` -> `candles`
- `strategies` -> `strategy_parameters`
- `signals` -> `trade_intents`
- `trade_intents` -> `orders`
- `orders` -> `fills`
- `fills` + `orders` -> `positions`
- `journal_entries` can reference both backtest and live trades

## Minimum Phase 1 Tables to Build First

Build first:
- `instruments`
- `instrument_mappings`
- `candles`
- `strategies`
- `signals`
- `trade_intents`
- `orders`
- `fills`
- `positions`
- `backtest_runs`
- `backtest_trades`
- `journal_entries`
- `raw_events`

Add soon after:
- `portfolio_snapshots`
- `trade_reviews`
- `corporate_actions`
- `trading_calendars`
- `data_ingestion_runs`
