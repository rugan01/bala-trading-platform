#!/usr/bin/env python3
"""
Brief End-of-Day Review
=======================
Scores archived morning-brief directional predictions and writes outcomes back
into the trading-platform archive so the next morning's learning summary has
real feedback to report.
"""

import os
import sys
import json
import argparse
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(TOOLS_DIR))

REPO_ROOT = Path(__file__).resolve().parents[2]
TRADING_PLATFORM_ROOT = REPO_ROOT / 'packages' / 'trading_platform'
TRADING_PLATFORM_SRC = TRADING_PLATFORM_ROOT / 'src'
if TRADING_PLATFORM_SRC.exists():
    sys.path.insert(0, str(TRADING_PLATFORM_SRC))

from fno_scanner import fetch_market_data, fetch_yahoo_data, get_upstox_provider
from trading_platform.archive.bootstrap import DEFAULT_DB_PATH
from trading_platform.briefs import BriefOutcomeRecord
from trading_platform.briefs.repository import archive_brief_outcomes, get_latest_brief_run, get_predictions_for_run
from trading_platform.paths import PREMARKET_REPORTS_ROOT

LOG_FILE = os.path.expanduser('~/Library/Logs/brief_eod_review.log')
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE)
    ]
)
logger = logging.getLogger(__name__)

DEFAULT_OUTPUT_DIR = PREMARKET_REPORTS_ROOT / 'review'
SUPPORTED_DIRECTIONAL_ASSET_CLASSES = {'equity', 'index', 'commodity'}
SUPPORTED_DIRECTIONS = {'bullish', 'bearish'}


def _is_directionally_correct(predicted_direction: str, outcome: BriefOutcomeRecord) -> bool:
    if predicted_direction == 'bullish':
        return bool(outcome.bullish_correct)
    if predicted_direction == 'bearish':
        return bool(outcome.bearish_correct)
    return False


def _fetch_quote_by_instrument_key(access_token: str, instrument_key: str) -> dict:
    response = requests.get(
        'https://api.upstox.com/v2/market-quote/quotes',
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {access_token}',
        },
        params={'instrument_key': instrument_key},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json().get('data', {})
    return next(iter(payload.values()), {})


def _purge_existing_outcomes(prediction_ids: List[str], evaluation_date: str) -> None:
    if not prediction_ids:
        return

    placeholders = ','.join('?' for _ in prediction_ids)
    with sqlite3.connect(DEFAULT_DB_PATH) as conn:
        conn.execute(
            f"DELETE FROM brief_outcomes WHERE evaluation_date = ? AND prediction_id IN ({placeholders})",
            [evaluation_date, *prediction_ids],
        )
        conn.commit()


def evaluate_equity_prediction(
    prediction: Dict[str, Any],
    evaluation_timestamp: datetime,
    *,
    provider=None,
) -> tuple[BriefOutcomeRecord | None, Optional[str]]:
    symbol = prediction.get('symbol')
    entry_reference = prediction.get('entry_reference')
    direction = prediction.get('predicted_direction')
    prediction_id = prediction.get('prediction_id')

    if not symbol or not entry_reference or direction not in SUPPORTED_DIRECTIONS or not prediction_id:
        return None, 'missing prediction fields'

    latest = fetch_market_data(
        symbol,
        '1y',
        source='upstox',
        mode='live',
        provider=provider,
        kind='equity',
    )
    data_source = 'upstox_live_quote'
    if latest and latest.get('suspected_corporate_action'):
        return None, 'skipped due to suspected corporate action / discontinuous price series'

    if not latest:
        latest = fetch_yahoo_data(symbol, '1mo')
        data_source = 'yahoo_fallback'
    if not latest:
        logger.warning('Could not fetch EOD data for %s', symbol)
        return None, 'data unavailable'

    current_price = latest.get('current_price')
    if current_price is None:
        return None, 'evaluated price unavailable'

    realized_return_pct = ((float(current_price) - float(entry_reference)) / float(entry_reference)) * 100
    bullish_correct = realized_return_pct > 0
    bearish_correct = realized_return_pct < 0
    signed_score = realized_return_pct if direction == 'bullish' else -realized_return_pct

    return (
        BriefOutcomeRecord(
            outcome_id=f"outcome_{prediction_id}_{evaluation_timestamp.date().isoformat()}",
            prediction_id=prediction_id,
            evaluation_timestamp=evaluation_timestamp.isoformat(),
            evaluation_date=evaluation_timestamp.date().isoformat(),
            horizon_label=str(prediction.get('horizon_label') or 'EOD'),
            realized_direction='bullish' if realized_return_pct > 0 else 'bearish' if realized_return_pct < 0 else 'flat',
            realized_return_pct=round(realized_return_pct, 4),
            bullish_correct=bullish_correct,
            bearish_correct=bearish_correct,
            score=round(signed_score, 4),
            notes=(
                f"EOD review via {data_source} for {symbol}. "
                f"Entry reference {entry_reference}, evaluated price {current_price}."
            ),
        ),
        None,
    )


def evaluate_index_prediction(
    prediction: Dict[str, Any],
    evaluation_timestamp: datetime,
    *,
    access_token: Optional[str],
) -> tuple[BriefOutcomeRecord | None, Optional[str]]:
    symbol = prediction.get('symbol')
    entry_reference = prediction.get('entry_reference')
    direction = prediction.get('predicted_direction')
    prediction_id = prediction.get('prediction_id')
    features = prediction.get('features') or {}
    instrument_key = features.get('instrument_key')

    if not symbol or not entry_reference or direction not in SUPPORTED_DIRECTIONS or not prediction_id:
        return None, 'missing prediction fields'
    if not instrument_key:
        return None, 'instrument key unavailable'
    if not access_token:
        return None, 'upstox access token unavailable'

    try:
        quote_payload = _fetch_quote_by_instrument_key(access_token, instrument_key)
    except Exception as exc:
        logger.warning('Could not fetch index quote for %s: %s', symbol, exc)
        return None, 'index quote unavailable'

    current_price = quote_payload.get('last_price')
    if current_price is None:
        return None, 'evaluated price unavailable'

    realized_return_pct = ((float(current_price) - float(entry_reference)) / float(entry_reference)) * 100
    bullish_correct = realized_return_pct > 0
    bearish_correct = realized_return_pct < 0
    signed_score = realized_return_pct if direction == 'bullish' else -realized_return_pct

    return (
        BriefOutcomeRecord(
            outcome_id=f"outcome_{prediction_id}_{evaluation_timestamp.date().isoformat()}",
            prediction_id=prediction_id,
            evaluation_timestamp=evaluation_timestamp.isoformat(),
            evaluation_date=evaluation_timestamp.date().isoformat(),
            horizon_label=str(prediction.get('horizon_label') or 'EOD'),
            realized_direction='bullish' if realized_return_pct > 0 else 'bearish' if realized_return_pct < 0 else 'flat',
            realized_return_pct=round(realized_return_pct, 4),
            bullish_correct=bullish_correct,
            bearish_correct=bearish_correct,
            score=round(signed_score, 4),
            notes=(
                f"EOD review via upstox_index_quote for {symbol}. "
                f"Entry reference {entry_reference}, evaluated price {current_price}."
            ),
        ),
        None,
    )


def evaluate_commodity_prediction(
    prediction: Dict[str, Any],
    evaluation_timestamp: datetime,
    *,
    access_token: Optional[str],
) -> tuple[BriefOutcomeRecord | None, Optional[str]]:
    symbol = prediction.get('symbol')
    entry_reference = prediction.get('entry_reference')
    direction = prediction.get('predicted_direction')
    prediction_id = prediction.get('prediction_id')
    features = prediction.get('features') or {}
    instrument_key = features.get('instrument_key') or ((features.get('instrument') or {}).get('instrument_key'))

    if not symbol or not entry_reference or direction not in SUPPORTED_DIRECTIONS or not prediction_id:
        return None, 'missing prediction fields'
    if not instrument_key:
        return None, 'commodity instrument key unavailable'
    if not access_token:
        return None, 'upstox access token unavailable'

    try:
        quote_payload = _fetch_quote_by_instrument_key(access_token, instrument_key)
    except Exception as exc:
        logger.warning('Could not fetch commodity quote for %s: %s', symbol, exc)
        return None, 'commodity quote unavailable'

    current_price = quote_payload.get('last_price')
    if current_price is None:
        return None, 'evaluated price unavailable'

    realized_return_pct = ((float(current_price) - float(entry_reference)) / float(entry_reference)) * 100
    bullish_correct = realized_return_pct > 0
    bearish_correct = realized_return_pct < 0
    signed_score = realized_return_pct if direction == 'bullish' else -realized_return_pct

    return (
        BriefOutcomeRecord(
            outcome_id=f"outcome_{prediction_id}_{evaluation_timestamp.date().isoformat()}",
            prediction_id=prediction_id,
            evaluation_timestamp=evaluation_timestamp.isoformat(),
            evaluation_date=evaluation_timestamp.date().isoformat(),
            horizon_label=str(prediction.get('horizon_label') or 'SESSION'),
            realized_direction='bullish' if realized_return_pct > 0 else 'bearish' if realized_return_pct < 0 else 'flat',
            realized_return_pct=round(realized_return_pct, 4),
            bullish_correct=bullish_correct,
            bearish_correct=bearish_correct,
            score=round(signed_score, 4),
            notes=(
                f"EOD review via upstox_commodity_quote for {symbol}. "
                f"Entry reference {entry_reference}, evaluated price {current_price}."
            ),
        ),
        None,
    )


def build_report(
    source_run: Dict[str, Any],
    prediction_map: Dict[str, Dict[str, Any]],
    evaluated: List[BriefOutcomeRecord],
    skipped: List[str],
    output_path: Path,
) -> Dict[str, Any]:
    total = len(evaluated)
    correct = 0
    for outcome in evaluated:
        prediction = prediction_map.get(outcome.prediction_id, {})
        if _is_directionally_correct(str(prediction.get('predicted_direction')), outcome):
            correct += 1

    summary = {
        'source_brief_run_id': source_run.get('brief_run_id'),
        'source_date': source_run.get('run_date'),
        'evaluated_count': total,
        'correct_count': correct,
        'hit_rate': round((correct / total) * 100, 2) if total else 0,
        'skipped_items': skipped,
        'outcomes': [
            {
                'prediction_id': outcome.prediction_id,
                'symbol': (prediction_map.get(outcome.prediction_id, {}) or {}).get('symbol'),
                'predicted_direction': (prediction_map.get(outcome.prediction_id, {}) or {}).get('predicted_direction'),
                'evaluation_date': outcome.evaluation_date,
                'realized_direction': outcome.realized_direction,
                'realized_return_pct': outcome.realized_return_pct,
                'bullish_correct': outcome.bullish_correct,
                'bearish_correct': outcome.bearish_correct,
                'score': outcome.score,
                'is_correct': _is_directionally_correct(
                    str((prediction_map.get(outcome.prediction_id, {}) or {}).get('predicted_direction')),
                    outcome,
                ),
                'notes': outcome.notes,
            }
            for outcome in evaluated
        ],
        'report_path': str(output_path),
    }
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description='Evaluate archived morning-brief directional calls at end of day')
    parser.add_argument('--source-date', type=str, default=datetime.now().date().isoformat(), help='Morning brief date to evaluate')
    parser.add_argument('--output', type=str, default=str(DEFAULT_OUTPUT_DIR), help='Directory to save the EOD review report')
    parser.add_argument('--no-archive', action='store_true', help='Skip writing outcomes into the archive')
    args = parser.parse_args()

    evaluation_timestamp = datetime.now()
    source_run = get_latest_brief_run(DEFAULT_DB_PATH, run_date=args.source_date)
    if source_run is None:
        logger.error('No archived morning brief run found for %s', args.source_date)
        return 1

    predictions = get_predictions_for_run(source_run['brief_run_id'], DEFAULT_DB_PATH)
    prediction_map = {prediction.get('prediction_id'): prediction for prediction in predictions if prediction.get('prediction_id')}
    evaluated: List[BriefOutcomeRecord] = []
    skipped: List[str] = []
    try:
        provider = get_upstox_provider()
    except Exception as exc:
        logger.warning('Could not initialize Upstox provider for EOD review, will use Yahoo fallback where needed: %s', exc)
        provider = None
    access_token = getattr(provider, 'access_token', None)

    for prediction in predictions:
        asset_class = prediction.get('asset_class')
        direction = prediction.get('predicted_direction')
        if asset_class not in SUPPORTED_DIRECTIONAL_ASSET_CLASSES or direction not in SUPPORTED_DIRECTIONS:
            skipped.append(f"{prediction.get('symbol')} ({asset_class}/{direction})")
            continue

        if asset_class == 'index':
            outcome, skip_reason = evaluate_index_prediction(
                prediction,
                evaluation_timestamp,
                access_token=access_token,
            )
        elif asset_class == 'commodity':
            outcome, skip_reason = evaluate_commodity_prediction(
                prediction,
                evaluation_timestamp,
                access_token=access_token,
            )
        else:
            outcome, skip_reason = evaluate_equity_prediction(
                prediction,
                evaluation_timestamp,
                provider=provider,
            )
        if outcome is None:
            skipped.append(f"{prediction.get('symbol')} ({skip_reason or 'data unavailable'})")
            continue
        evaluated.append(outcome)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    basename = f"brief_eod_review_{evaluation_timestamp.strftime('%Y%m%d_%H%M')}"
    text_path = output_dir / f'{basename}.txt'
    json_path = output_dir / f'{basename}.json'

    if not args.no_archive and evaluated:
        _purge_existing_outcomes(list(prediction_map.keys()), evaluation_timestamp.date().isoformat())
        archive_brief_outcomes(evaluated, DEFAULT_DB_PATH)

    summary = build_report(source_run, prediction_map, evaluated, skipped, text_path)
    lines = [
        'BRIEF END-OF-DAY REVIEW',
        '=' * 80,
        f"Source Brief Run: {source_run.get('brief_run_id')} | {source_run.get('run_timestamp')}",
        f"Evaluated directional calls: {summary['evaluated_count']}",
        f"Correct calls: {summary['correct_count']} | Hit Rate: {summary['hit_rate']:.2f}%",
        '',
        'OUTCOMES',
        '-' * 80,
    ]

    for item in summary['outcomes']:
        lines.append(
            f"{item['prediction_id']}: {item['symbol']} | predicted {item['predicted_direction']} -> realized {item['realized_direction']} | "
            f"{'CORRECT' if item['is_correct'] else 'WRONG'} | return {item['realized_return_pct']:+.2f}% | score {item['score']:+.2f}"
        )

    if skipped:
        lines.extend(['', 'SKIPPED', '-' * 80])
        lines.extend(skipped)

    text_report = '\n'.join(lines)
    text_path.write_text(text_report, encoding='utf-8')
    json_path.write_text(json.dumps(summary, indent=2), encoding='utf-8')

    print(text_report)
    logger.info('EOD review saved to %s', text_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
