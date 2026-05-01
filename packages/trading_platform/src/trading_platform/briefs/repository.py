from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4

from trading_platform.archive.bootstrap import DEFAULT_DB_PATH, initialize_database
from trading_platform.briefs.models import (
    BriefOutcomeRecord,
    BriefPredictionRecord,
    BriefRunRecord,
    LiveAnalysisCheckRecord,
    LiveAnalysisRunRecord,
)


def _ensure_db(db_path: Path) -> sqlite3.Connection:
    initialize_database(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _json(data: dict | None) -> str | None:
    if data is None:
        return None
    return json.dumps(data, sort_keys=True)


def _loads_json(raw: str | None):
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _row_to_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    for key in ('metadata_json', 'features_json', 'details_json'):
        if key in data:
            parsed = _loads_json(data[key])
            clean_key = key.removesuffix('_json')
            data[clean_key] = parsed
            del data[key]
    return data


def archive_brief_run(
    run: BriefRunRecord,
    predictions: list[BriefPredictionRecord],
    db_path: Path = DEFAULT_DB_PATH,
) -> str:
    run_id = run.brief_run_id or f'brief_run_{uuid4().hex}'

    with _ensure_db(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO brief_runs (
                brief_run_id, run_timestamp, run_date, session_label, mode, environment,
                market_phase, source_version, output_path, summary_text,
                learning_summary_text, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                run.run_timestamp,
                run.run_date,
                run.session_label,
                run.mode,
                run.environment,
                run.market_phase,
                run.source_version,
                run.output_path,
                run.summary_text,
                run.learning_summary_text,
                _json(run.metadata),
            ),
        )

        for prediction in predictions:
            prediction_id = prediction.prediction_id or f'prediction_{uuid4().hex}'
            conn.execute(
                """
                INSERT OR REPLACE INTO brief_predictions (
                    prediction_id, brief_run_id, asset_class, universe, symbol, timeframe,
                    horizon_label, signal_family, predicted_direction, confidence_score,
                    expected_move_pct, setup_quality, regime_label, recommendation_text,
                    entry_reference, stop_reference, target_reference, invalidation_text,
                    features_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prediction_id,
                    run_id,
                    prediction.asset_class,
                    prediction.universe,
                    prediction.symbol,
                    prediction.timeframe,
                    prediction.horizon_label,
                    prediction.signal_family,
                    prediction.predicted_direction,
                    prediction.confidence_score,
                    prediction.expected_move_pct,
                    prediction.setup_quality,
                    prediction.regime_label,
                    prediction.recommendation_text,
                    prediction.entry_reference,
                    prediction.stop_reference,
                    prediction.target_reference,
                    prediction.invalidation_text,
                    _json(prediction.features),
                    _json(prediction.metadata),
                ),
            )

        conn.commit()

    return run_id


def archive_brief_outcomes(
    outcomes: list[BriefOutcomeRecord],
    db_path: Path = DEFAULT_DB_PATH,
) -> int:
    with _ensure_db(db_path) as conn:
        for outcome in outcomes:
            outcome_id = outcome.outcome_id or f'outcome_{uuid4().hex}'
            conn.execute(
                """
                INSERT OR REPLACE INTO brief_outcomes (
                    outcome_id, prediction_id, evaluation_timestamp, evaluation_date,
                    horizon_label, realized_direction, realized_return_pct,
                    max_favorable_excursion_pct, max_adverse_excursion_pct,
                    hit_target, hit_stop, bullish_correct, bearish_correct, score, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    outcome_id,
                    outcome.prediction_id,
                    outcome.evaluation_timestamp,
                    outcome.evaluation_date,
                    outcome.horizon_label,
                    outcome.realized_direction,
                    outcome.realized_return_pct,
                    outcome.max_favorable_excursion_pct,
                    outcome.max_adverse_excursion_pct,
                    None if outcome.hit_target is None else int(outcome.hit_target),
                    None if outcome.hit_stop is None else int(outcome.hit_stop),
                    None if outcome.bullish_correct is None else int(outcome.bullish_correct),
                    None if outcome.bearish_correct is None else int(outcome.bearish_correct),
                    outcome.score,
                    outcome.notes,
                ),
            )

        conn.commit()

    return len(outcomes)


def archive_live_analysis(
    run: LiveAnalysisRunRecord,
    checks: list[LiveAnalysisCheckRecord],
    db_path: Path = DEFAULT_DB_PATH,
) -> str:
    run_id = run.live_analysis_run_id or f'live_analysis_{uuid4().hex}'

    with _ensure_db(db_path) as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO live_analysis_runs (
                live_analysis_run_id, source_brief_run_id, run_timestamp, run_date,
                market_phase, overall_status, summary_text, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                run.source_brief_run_id,
                run.run_timestamp,
                run.run_date,
                run.market_phase,
                run.overall_status,
                run.summary_text,
                _json(run.metadata),
            ),
        )

        for check in checks:
            check_id = check.check_id or f'live_check_{uuid4().hex}'
            conn.execute(
                """
                INSERT OR REPLACE INTO live_analysis_checks (
                    check_id, live_analysis_run_id, scope, symbol, thesis_status,
                    current_price, reference_price, delta_pct, summary_text, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    check_id,
                    run_id,
                    check.scope,
                    check.symbol,
                    check.thesis_status,
                    check.current_price,
                    check.reference_price,
                    check.delta_pct,
                    check.summary_text,
                    _json(check.details),
                ),
            )

        conn.commit()

    return run_id


def get_latest_brief_run(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    session_label: str = 'morning_brief',
    run_date: str | None = None,
) -> dict | None:
    query = 'SELECT * FROM brief_runs WHERE session_label = ?'
    params: list[str] = [session_label]
    if run_date:
        query += ' AND run_date = ?'
        params.append(run_date)
    query += ' ORDER BY run_timestamp DESC LIMIT 1'

    with _ensure_db(db_path) as conn:
        row = conn.execute(query, params).fetchone()
        return _row_to_dict(row) if row else None


def get_predictions_for_run(
    brief_run_id: str,
    db_path: Path = DEFAULT_DB_PATH,
) -> list[dict]:
    with _ensure_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM brief_predictions
            WHERE brief_run_id = ?
            ORDER BY universe, signal_family, symbol, prediction_id
            """,
            (brief_run_id,),
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def summarize_recent_learning(
    db_path: Path = DEFAULT_DB_PATH,
    *,
    lookback_days: int = 20,
    max_families: int = 2,
) -> str:
    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()

    with _ensure_db(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                p.signal_family,
                p.predicted_direction,
                o.bullish_correct,
                o.bearish_correct,
                o.score
            FROM brief_outcomes o
            JOIN brief_predictions p ON p.prediction_id = o.prediction_id
            WHERE o.evaluation_date >= ?
              AND p.predicted_direction IN ('bullish', 'bearish')
            """,
            (cutoff,),
        ).fetchall()

    if not rows:
        return (
            'No evaluated brief predictions yet. The archive is ready; once end-of-day reviews start '
            'writing outcomes, this section will summarize hit rates and what worked yesterday.'
        )

    def is_correct(row: sqlite3.Row) -> bool:
        if row['predicted_direction'] == 'bullish':
            return bool(row['bullish_correct'])
        if row['predicted_direction'] == 'bearish':
            return bool(row['bearish_correct'])
        return False

    total = len(rows)
    correct = sum(1 for row in rows if is_correct(row))
    bullish_rows = [row for row in rows if row['predicted_direction'] == 'bullish']
    bearish_rows = [row for row in rows if row['predicted_direction'] == 'bearish']

    family_stats: dict[str, dict[str, float]] = {}
    for row in rows:
        stats = family_stats.setdefault(row['signal_family'], {'total': 0, 'correct': 0})
        stats['total'] += 1
        if is_correct(row):
            stats['correct'] += 1

    ranked_families = sorted(
        family_stats.items(),
        key=lambda item: ((item[1]['correct'] / item[1]['total']) if item[1]['total'] else 0, item[1]['total']),
        reverse=True,
    )

    lines = [
        f"Recent directional learning: {correct}/{total} correct ({(correct / total) * 100:.1f}% hit rate) over the last {lookback_days} days.",
    ]

    if bullish_rows:
        bullish_correct = sum(1 for row in bullish_rows if is_correct(row))
        lines.append(
            f"Bullish precision: {bullish_correct}/{len(bullish_rows)} ({(bullish_correct / len(bullish_rows)) * 100:.1f}%)."
        )
    if bearish_rows:
        bearish_correct = sum(1 for row in bearish_rows if is_correct(row))
        lines.append(
            f"Bearish precision: {bearish_correct}/{len(bearish_rows)} ({(bearish_correct / len(bearish_rows)) * 100:.1f}%)."
        )

    if ranked_families:
        highlights = []
        for family, stats in ranked_families[:max_families]:
            hit_rate = (stats['correct'] / stats['total']) * 100 if stats['total'] else 0
            highlights.append(f"{family}: {hit_rate:.1f}% ({int(stats['correct'])}/{int(stats['total'])})")
        lines.append('Best recent signal families: ' + '; '.join(highlights) + '.')

    weakest = [item for item in reversed(ranked_families) if item[1]['total'] >= 2]
    if weakest:
        family, stats = weakest[0]
        hit_rate = (stats['correct'] / stats['total']) * 100 if stats['total'] else 0
        lines.append(f"Weakest recent family: {family} at {hit_rate:.1f}% ({int(stats['correct'])}/{int(stats['total'])}).")

    return '\n'.join(lines)
