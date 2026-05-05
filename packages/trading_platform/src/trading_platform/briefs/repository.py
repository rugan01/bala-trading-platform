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
                    hit_target, hit_stop, bullish_correct, bearish_correct, score, notes, details_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    _json(outcome.details),
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
                o.score,
                o.evaluation_date,
                o.details_json
            FROM brief_outcomes o
            JOIN brief_predictions p ON p.prediction_id = o.prediction_id
            WHERE o.evaluation_date >= ?
              AND (
                    p.predicted_direction IN ('bullish', 'bearish')
                    OR p.predicted_direction LIKE '%bullish%'
                    OR p.predicted_direction LIKE '%bearish%'
                  )
            """,
            (cutoff,),
        ).fetchall()

    if not rows:
        return (
            'No evaluated brief predictions yet. The archive is ready; once end-of-day reviews start '
            'writing outcomes, this section will summarize hit rates and what worked yesterday.'
        )

    def is_correct(row: sqlite3.Row) -> bool:
        direction = str(row['predicted_direction'] or '').lower()
        if 'bullish' in direction:
            return bool(row['bullish_correct'])
        if 'bearish' in direction:
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

    latest_date = max((row['evaluation_date'] for row in rows if row['evaluation_date']), default=None)
    latest_rows = [row for row in rows if row['evaluation_date'] == latest_date] if latest_date else []
    parsed_details = []
    for row in latest_rows:
        details = _loads_json(row['details_json']) if 'details_json' in row.keys() else None
        if details:
            parsed_details.append(details)

    if parsed_details:
        intraday_counts: dict[str, int] = {}
        open_relation_counts: dict[str, int] = {}
        cpr_bucket_counts: dict[str, int] = {}
        gap_gt_1 = 0
        rejection_count = 0
        trended_open_relations: dict[str, int] = {}
        sideways_open_relations: dict[str, int] = {}
        trended_gap_gt_1 = 0
        sideways_gap_gt_1 = 0
        for details in parsed_details:
            intraday = ((details.get('intraday') or {}).get('intraday_character')) or 'unknown'
            intraday_counts[intraday] = intraday_counts.get(intraday, 0) + 1

            structure = details.get('day_structure') or {}
            open_relation = structure.get('open_relation') or 'unknown'
            open_relation_counts[open_relation] = open_relation_counts.get(open_relation, 0) + 1

            cpr_bucket = structure.get('cpr_width_bucket') or 'unknown'
            cpr_bucket_counts[cpr_bucket] = cpr_bucket_counts.get(cpr_bucket, 0) + 1

            if structure.get('gap_gt_1pct'):
                gap_gt_1 += 1
                if intraday == 'trended':
                    trended_gap_gt_1 += 1
                if intraday == 'sideways':
                    sideways_gap_gt_1 += 1

            if intraday == 'trended':
                trended_open_relations[open_relation] = trended_open_relations.get(open_relation, 0) + 1
            elif intraday == 'sideways':
                sideways_open_relations[open_relation] = sideways_open_relations.get(open_relation, 0) + 1

            rejections = structure.get('camarilla_rejections') or {}
            if any(bool(value) for value in rejections.values()):
                rejection_count += 1

        top_open_relation = max(open_relation_counts.items(), key=lambda item: item[1])[0] if open_relation_counts else 'unknown'
        top_cpr_bucket = max(cpr_bucket_counts.items(), key=lambda item: item[1])[0] if cpr_bucket_counts else 'unknown'
        lines.append(
            f"Latest reviewed day ({latest_date}) structure mix: "
            f"trended={intraday_counts.get('trended', 0)}, sideways={intraday_counts.get('sideways', 0)}, "
            f"moved_opposite={intraday_counts.get('moved_opposite', 0)}, two_sided_volatile={intraday_counts.get('two_sided_volatile', 0)}."
        )
        lines.append(
            f"Common day traits on {latest_date}: most names opened {top_open_relation}, "
            f"most had {top_cpr_bucket} CPR width, {gap_gt_1}/{len(parsed_details)} had >1% gap, "
            f"and {rejection_count}/{len(parsed_details)} showed Camarilla rejection behavior."
        )
        if trended_open_relations:
            top_trended_open = max(trended_open_relations.items(), key=lambda item: item[1])[0]
            lines.append(
                f"Trending names on {latest_date} most often opened {top_trended_open}; "
                f"{trended_gap_gt_1}/{intraday_counts.get('trended', 0)} of them had >1% gaps."
            )
        if sideways_open_relations:
            top_sideways_open = max(sideways_open_relations.items(), key=lambda item: item[1])[0]
            lines.append(
                f"Sideways names on {latest_date} most often opened {top_sideways_open}; "
                f"{sideways_gap_gt_1}/{intraday_counts.get('sideways', 0)} of them had >1% gaps."
            )

    return '\n'.join(lines)
