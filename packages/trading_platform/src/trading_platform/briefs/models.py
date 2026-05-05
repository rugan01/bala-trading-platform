from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BriefRunRecord:
    run_timestamp: str
    run_date: str
    session_label: str
    mode: str
    market_phase: str
    environment: str = "production"
    source_version: str | None = None
    output_path: str | None = None
    summary_text: str | None = None
    learning_summary_text: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    brief_run_id: str | None = None


@dataclass(slots=True)
class BriefPredictionRecord:
    asset_class: str
    universe: str
    symbol: str
    timeframe: str
    horizon_label: str
    signal_family: str
    predicted_direction: str
    recommendation_text: str
    confidence_score: float | None = None
    expected_move_pct: float | None = None
    setup_quality: float | None = None
    regime_label: str | None = None
    entry_reference: float | None = None
    stop_reference: float | None = None
    target_reference: float | None = None
    invalidation_text: str | None = None
    features: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    prediction_id: str | None = None


@dataclass(slots=True)
class BriefOutcomeRecord:
    prediction_id: str
    evaluation_timestamp: str
    evaluation_date: str
    horizon_label: str
    realized_direction: str | None = None
    realized_return_pct: float | None = None
    max_favorable_excursion_pct: float | None = None
    max_adverse_excursion_pct: float | None = None
    hit_target: bool | None = None
    hit_stop: bool | None = None
    bullish_correct: bool | None = None
    bearish_correct: bool | None = None
    score: float | None = None
    notes: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    outcome_id: str | None = None


@dataclass(slots=True)
class LiveAnalysisRunRecord:
    source_brief_run_id: str
    run_timestamp: str
    run_date: str
    market_phase: str
    overall_status: str
    summary_text: str
    metadata: dict[str, Any] = field(default_factory=dict)
    live_analysis_run_id: str | None = None


@dataclass(slots=True)
class LiveAnalysisCheckRecord:
    scope: str
    thesis_status: str
    summary_text: str
    symbol: str | None = None
    current_price: float | None = None
    reference_price: float | None = None
    delta_pct: float | None = None
    details: dict[str, Any] = field(default_factory=dict)
    check_id: str | None = None
