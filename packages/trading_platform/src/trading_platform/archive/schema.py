from __future__ import annotations

PRAGMAS = [
    "PRAGMA journal_mode=WAL;",
    "PRAGMA foreign_keys=ON;",
]

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS brokers (
        broker_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounts (
        account_id TEXT PRIMARY KEY,
        broker_id TEXT NOT NULL,
        display_name TEXT NOT NULL,
        account_type TEXT,
        base_currency TEXT DEFAULT 'INR',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (broker_id) REFERENCES brokers (broker_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS instruments (
        instrument_key TEXT PRIMARY KEY,
        venue TEXT NOT NULL,
        segment TEXT,
        symbol TEXT NOT NULL,
        trading_symbol TEXT,
        instrument_type TEXT,
        underlying_symbol TEXT,
        expiry_date TEXT,
        strike REAL,
        option_type TEXT,
        lot_size INTEGER,
        tick_size REAL,
        isin TEXT,
        exchange_token TEXT,
        snapshot_date TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS instrument_aliases (
        alias_key TEXT PRIMARY KEY,
        instrument_key TEXT NOT NULL,
        source TEXT NOT NULL,
        alias_value TEXT NOT NULL,
        valid_from TEXT,
        valid_to TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (instrument_key) REFERENCES instruments (instrument_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS archive_runs (
        run_id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        account_id TEXT,
        run_date TEXT,
        mode TEXT NOT NULL,
        status TEXT NOT NULL,
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        finished_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS raw_payloads (
        payload_id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        source TEXT NOT NULL,
        endpoint TEXT NOT NULL,
        trade_date TEXT,
        account_id TEXT,
        payload_sha256 TEXT,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (run_id) REFERENCES archive_runs (run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_orders (
        order_uid TEXT PRIMARY KEY,
        broker_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        broker_order_id TEXT,
        strategy_id TEXT,
        instrument_key TEXT,
        trading_symbol TEXT,
        side TEXT NOT NULL,
        product_type TEXT,
        order_type TEXT,
        quantity INTEGER NOT NULL,
        limit_price REAL,
        trigger_price REAL,
        status TEXT,
        order_timestamp TEXT,
        exchange_timestamp TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (broker_id) REFERENCES brokers (broker_id),
        FOREIGN KEY (account_id) REFERENCES accounts (account_id),
        FOREIGN KEY (instrument_key) REFERENCES instruments (instrument_key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS trade_fills (
        fill_uid TEXT PRIMARY KEY,
        broker_id TEXT NOT NULL,
        account_id TEXT NOT NULL,
        broker_trade_id TEXT,
        broker_order_id TEXT,
        instrument_key TEXT,
        trading_symbol TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        fill_timestamp TEXT,
        side TEXT NOT NULL,
        quantity INTEGER NOT NULL,
        price REAL NOT NULL,
        amount REAL,
        lot_size INTEGER,
        fees REAL,
        source TEXT NOT NULL,
        source_run_id TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (broker_id) REFERENCES brokers (broker_id),
        FOREIGN KEY (account_id) REFERENCES accounts (account_id),
        FOREIGN KEY (instrument_key) REFERENCES instruments (instrument_key),
        FOREIGN KEY (source_run_id) REFERENCES archive_runs (run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS journal_links (
        journal_key TEXT PRIMARY KEY,
        account_id TEXT NOT NULL,
        notion_page_id TEXT,
        broker_id TEXT,
        first_trade_date TEXT,
        last_trade_date TEXT,
        status TEXT,
        linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brief_runs (
        brief_run_id TEXT PRIMARY KEY,
        run_timestamp TEXT NOT NULL,
        run_date TEXT NOT NULL,
        session_label TEXT NOT NULL,
        mode TEXT NOT NULL,
        environment TEXT NOT NULL DEFAULT 'production',
        market_phase TEXT NOT NULL,
        source_version TEXT,
        output_path TEXT,
        summary_text TEXT,
        learning_summary_text TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brief_predictions (
        prediction_id TEXT PRIMARY KEY,
        brief_run_id TEXT NOT NULL,
        asset_class TEXT NOT NULL,
        universe TEXT NOT NULL,
        symbol TEXT NOT NULL,
        timeframe TEXT NOT NULL,
        horizon_label TEXT NOT NULL,
        signal_family TEXT NOT NULL,
        predicted_direction TEXT NOT NULL,
        confidence_score REAL,
        expected_move_pct REAL,
        setup_quality REAL,
        regime_label TEXT,
        recommendation_text TEXT,
        entry_reference REAL,
        stop_reference REAL,
        target_reference REAL,
        invalidation_text TEXT,
        features_json TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (brief_run_id) REFERENCES brief_runs (brief_run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brief_outcomes (
        outcome_id TEXT PRIMARY KEY,
        prediction_id TEXT NOT NULL,
        evaluation_timestamp TEXT NOT NULL,
        evaluation_date TEXT NOT NULL,
        horizon_label TEXT NOT NULL,
        realized_direction TEXT,
        realized_return_pct REAL,
        max_favorable_excursion_pct REAL,
        max_adverse_excursion_pct REAL,
        hit_target INTEGER,
        hit_stop INTEGER,
        bullish_correct INTEGER,
        bearish_correct INTEGER,
        score REAL,
        notes TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (prediction_id) REFERENCES brief_predictions (prediction_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS brief_learning_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        created_at TEXT NOT NULL,
        scope TEXT NOT NULL,
        model_family TEXT NOT NULL,
        sample_size INTEGER NOT NULL,
        hit_rate REAL,
        precision_bullish REAL,
        precision_bearish REAL,
        brier_score REAL,
        calibration_error REAL,
        notes TEXT,
        metrics_json TEXT,
        recommended_adjustments_json TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_analysis_runs (
        live_analysis_run_id TEXT PRIMARY KEY,
        source_brief_run_id TEXT NOT NULL,
        run_timestamp TEXT NOT NULL,
        run_date TEXT NOT NULL,
        market_phase TEXT NOT NULL,
        overall_status TEXT NOT NULL,
        summary_text TEXT,
        metadata_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (source_brief_run_id) REFERENCES brief_runs (brief_run_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS live_analysis_checks (
        check_id TEXT PRIMARY KEY,
        live_analysis_run_id TEXT NOT NULL,
        scope TEXT NOT NULL,
        symbol TEXT,
        thesis_status TEXT NOT NULL,
        current_price REAL,
        reference_price REAL,
        delta_pct REAL,
        summary_text TEXT,
        details_json TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (live_analysis_run_id) REFERENCES live_analysis_runs (live_analysis_run_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_trade_fills_account_date ON trade_fills(account_id, trade_date);",
    "CREATE INDEX IF NOT EXISTS idx_trade_fills_broker_trade ON trade_fills(broker_id, broker_trade_id);",
    "CREATE INDEX IF NOT EXISTS idx_trade_orders_account_time ON trade_orders(account_id, order_timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_instrument_aliases_lookup ON instrument_aliases(source, alias_value);",
    "CREATE INDEX IF NOT EXISTS idx_brief_runs_run_date ON brief_runs(run_date, run_timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_brief_predictions_symbol ON brief_predictions(symbol, horizon_label);",
    "CREATE INDEX IF NOT EXISTS idx_brief_predictions_run ON brief_predictions(brief_run_id);",
    "CREATE INDEX IF NOT EXISTS idx_brief_outcomes_prediction ON brief_outcomes(prediction_id);",
    "CREATE INDEX IF NOT EXISTS idx_brief_learning_scope ON brief_learning_snapshots(scope, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_live_analysis_runs_source ON live_analysis_runs(source_brief_run_id, run_timestamp);",
    "CREATE INDEX IF NOT EXISTS idx_live_analysis_checks_run ON live_analysis_checks(live_analysis_run_id, scope, symbol);",
]
