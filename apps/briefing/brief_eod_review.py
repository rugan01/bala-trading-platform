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
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

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
SUPPORTED_DIRECTIONAL_ASSET_CLASSES = {'equity', 'index', 'commodity', 'macro'}
SUPPORTED_DIRECTIONS = {'bullish', 'bearish'}
DEFAULT_TREND_THRESHOLD_PCT = 1.0


def _is_directionally_correct(predicted_direction: str, outcome: BriefOutcomeRecord) -> bool:
    normalized = _normalize_predicted_direction(predicted_direction)
    if normalized == 'bullish':
        return bool(outcome.bullish_correct)
    if normalized == 'bearish':
        return bool(outcome.bearish_correct)
    return False


def _normalize_predicted_direction(direction: str | None) -> str:
    text = str(direction or '').strip().lower().replace(' ', '_')
    if not text:
        return ''
    if text in SUPPORTED_DIRECTIONS:
        return text
    if 'bullish' in text:
        return 'bullish'
    if 'bearish' in text:
        return 'bearish'
    return text


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


def _extract_candle_date(value: object) -> Optional[date]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    iso_candidate = text.replace('Z', '+00:00')
    try:
        return datetime.fromisoformat(iso_candidate).date()
    except ValueError:
        pass

    for fmt in ('%Y-%m-%d', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(text[:19], fmt).date()
        except ValueError:
            continue
    return None


def _fetch_daily_candles(
    access_token: str,
    instrument_key: str,
    *,
    to_date: date,
    lookback_days: int = 12,
) -> List[Dict[str, Any]]:
    from_date = to_date - timedelta(days=max(lookback_days * 3, 20))
    url = (
        f'https://api.upstox.com/v2/historical-candle/'
        f'{quote(instrument_key, safe="")}/day/{to_date.isoformat()}/{from_date.isoformat()}'
    )
    response = requests.get(
        url,
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {access_token}',
        },
        timeout=20,
    )
    response.raise_for_status()
    candles = response.json().get('data', {}).get('candles', [])
    parsed: List[Dict[str, Any]] = []
    for candle in reversed(candles):
        parsed.append(
            {
                'date': candle[0],
                'open': float(candle[1]),
                'high': float(candle[2]),
                'low': float(candle[3]),
                'close': float(candle[4]),
                'volume': int(candle[5] or 0),
            }
        )
    return parsed


def _get_day_candle_context(
    access_token: Optional[str],
    instrument_key: str,
    target_date: date,
) -> Optional[Dict[str, Any]]:
    if not access_token:
        return None

    try:
        candles = _fetch_daily_candles(access_token, instrument_key, to_date=target_date)
    except Exception as exc:
        logger.warning('Could not fetch historical day candles for %s: %s', instrument_key, exc)
        return None

    for idx, candle in enumerate(candles):
        candle_date = _extract_candle_date(candle.get('date'))
        if candle_date != target_date:
            continue
        previous = candles[idx - 1] if idx > 0 else None
        return {
            'date': candle_date.isoformat(),
            'open': float(candle['open']),
            'high': float(candle['high']),
            'low': float(candle['low']),
            'close': float(candle['close']),
            'previous_close': float(previous['close']) if previous else None,
        }
    return None


def _compute_intraday_characteristics(
    *,
    predicted_direction: str,
    day_context: Dict[str, Any],
    trend_threshold_pct: float,
) -> Dict[str, Any]:
    day_open = float(day_context['open'])
    day_high = float(day_context['high'])
    day_low = float(day_context['low'])
    day_close = float(day_context['close'])
    previous_close = day_context.get('previous_close')

    favorable_pct = 0.0
    adverse_pct = 0.0
    close_from_open_pct = 0.0
    gap_pct = None

    if day_open:
        if predicted_direction == 'bullish':
            favorable_pct = ((day_high - day_open) / day_open) * 100
            adverse_pct = ((day_open - day_low) / day_open) * 100
        else:
            favorable_pct = ((day_open - day_low) / day_open) * 100
            adverse_pct = ((day_high - day_open) / day_open) * 100
        close_from_open_pct = ((day_close - day_open) / day_open) * 100
    if previous_close:
        gap_pct = ((day_open - float(previous_close)) / float(previous_close)) * 100

    if favorable_pct >= trend_threshold_pct and adverse_pct >= trend_threshold_pct:
        intraday_character = 'two_sided_volatile'
    elif favorable_pct >= trend_threshold_pct:
        intraday_character = 'trended'
    elif adverse_pct >= trend_threshold_pct:
        intraday_character = 'moved_opposite'
    else:
        intraday_character = 'sideways'

    return {
        'day_open': round(day_open, 4),
        'day_high': round(day_high, 4),
        'day_low': round(day_low, 4),
        'day_close': round(day_close, 4),
        'previous_close': round(float(previous_close), 4) if previous_close is not None else None,
        'gap_pct': round(gap_pct, 4) if gap_pct is not None else None,
        'favorable_move_pct_from_open': round(favorable_pct, 4),
        'adverse_move_pct_from_open': round(adverse_pct, 4),
        'close_from_open_pct': round(close_from_open_pct, 4),
        'intraday_character': intraday_character,
        'trend_threshold_pct': trend_threshold_pct,
    }


def _build_outcome_notes(
    *,
    source_label: str,
    symbol: str,
    entry_reference: float,
    evaluated_price: float,
    intraday: Optional[Dict[str, Any]],
) -> str:
    base = (
        f"EOD review via {source_label} for {symbol}. "
        f"Entry reference {entry_reference}, evaluated price {evaluated_price}."
    )
    if not intraday:
        return base
    return (
        f"{base} Intraday character {intraday['intraday_character']}; "
        f"gap {intraday['gap_pct']:+.2f}% | favorable-from-open {intraday['favorable_move_pct_from_open']:+.2f}% | "
        f"adverse-from-open {intraday['adverse_move_pct_from_open']:+.2f}%."
    )


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
    evaluation_date: date,
    *,
    provider=None,
    access_token: Optional[str] = None,
    trend_threshold_pct: float = DEFAULT_TREND_THRESHOLD_PCT,
) -> tuple[BriefOutcomeRecord | None, Optional[str]]:
    symbol = prediction.get('symbol')
    entry_reference = prediction.get('entry_reference')
    direction = _normalize_predicted_direction(prediction.get('predicted_direction'))
    prediction_id = prediction.get('prediction_id')
    features = prediction.get('features') or {}

    if not symbol or not entry_reference or direction not in SUPPORTED_DIRECTIONS or not prediction_id:
        return None, 'missing prediction fields'

    current_price = None
    day_context = None
    instrument_key = features.get('instrument_key')
    data_source = 'upstox_day_candle'

    if not instrument_key and provider is not None:
        try:
            instrument_key = provider.resolve_symbol(symbol, kind='equity').get('instrument_key')
        except Exception:
            instrument_key = None

    if instrument_key:
        day_context = _get_day_candle_context(access_token, instrument_key, evaluation_date)
        if day_context:
            current_price = day_context.get('close')

    if current_price is None:
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
        if not instrument_key:
            instrument_key = latest.get('instrument_key')
        if instrument_key and day_context is None:
            day_context = _get_day_candle_context(access_token, instrument_key, evaluation_date)

    realized_return_pct = ((float(current_price) - float(entry_reference)) / float(entry_reference)) * 100
    bullish_correct = realized_return_pct > 0
    bearish_correct = realized_return_pct < 0
    signed_score = realized_return_pct if direction == 'bullish' else -realized_return_pct
    intraday = (
        _compute_intraday_characteristics(
            predicted_direction=direction,
            day_context=day_context,
            trend_threshold_pct=trend_threshold_pct,
        )
        if day_context
        else None
    )

    return (
        BriefOutcomeRecord(
            outcome_id=f"outcome_{prediction_id}_{evaluation_date.isoformat()}",
            prediction_id=prediction_id,
            evaluation_timestamp=evaluation_timestamp.isoformat(),
            evaluation_date=evaluation_date.isoformat(),
            horizon_label=str(prediction.get('horizon_label') or 'EOD'),
            realized_direction='bullish' if realized_return_pct > 0 else 'bearish' if realized_return_pct < 0 else 'flat',
            realized_return_pct=round(realized_return_pct, 4),
            max_favorable_excursion_pct=(intraday or {}).get('favorable_move_pct_from_open'),
            max_adverse_excursion_pct=(intraday or {}).get('adverse_move_pct_from_open'),
            bullish_correct=bullish_correct,
            bearish_correct=bearish_correct,
            score=round(signed_score, 4),
            notes=_build_outcome_notes(
                source_label=data_source,
                symbol=symbol,
                entry_reference=float(entry_reference),
                evaluated_price=float(current_price),
                intraday=intraday,
            ),
        ),
        None,
    )


def evaluate_index_prediction(
    prediction: Dict[str, Any],
    evaluation_timestamp: datetime,
    evaluation_date: date,
    *,
    access_token: Optional[str],
    trend_threshold_pct: float = DEFAULT_TREND_THRESHOLD_PCT,
) -> tuple[BriefOutcomeRecord | None, Optional[str]]:
    symbol = prediction.get('symbol')
    entry_reference = prediction.get('entry_reference')
    direction = _normalize_predicted_direction(prediction.get('predicted_direction'))
    prediction_id = prediction.get('prediction_id')
    features = prediction.get('features') or {}
    instrument_key = features.get('instrument_key')

    if not symbol or not entry_reference or direction not in SUPPORTED_DIRECTIONS or not prediction_id:
        return None, 'missing prediction fields'
    if not instrument_key:
        return None, 'instrument key unavailable'
    if not access_token:
        return None, 'upstox access token unavailable'

    day_context = _get_day_candle_context(access_token, instrument_key, evaluation_date)
    current_price = day_context.get('close') if day_context else None
    source_label = 'upstox_index_day_candle'
    if current_price is None:
        try:
            quote_payload = _fetch_quote_by_instrument_key(access_token, instrument_key)
        except Exception as exc:
            logger.warning('Could not fetch index quote for %s: %s', symbol, exc)
            return None, 'index quote unavailable'
        current_price = quote_payload.get('last_price')
        source_label = 'upstox_index_quote'
        if current_price is None:
            return None, 'evaluated price unavailable'

    realized_return_pct = ((float(current_price) - float(entry_reference)) / float(entry_reference)) * 100
    bullish_correct = realized_return_pct > 0
    bearish_correct = realized_return_pct < 0
    signed_score = realized_return_pct if direction == 'bullish' else -realized_return_pct
    intraday = (
        _compute_intraday_characteristics(
            predicted_direction=direction,
            day_context=day_context,
            trend_threshold_pct=trend_threshold_pct,
        )
        if day_context
        else None
    )

    return (
        BriefOutcomeRecord(
            outcome_id=f"outcome_{prediction_id}_{evaluation_date.isoformat()}",
            prediction_id=prediction_id,
            evaluation_timestamp=evaluation_timestamp.isoformat(),
            evaluation_date=evaluation_date.isoformat(),
            horizon_label=str(prediction.get('horizon_label') or 'EOD'),
            realized_direction='bullish' if realized_return_pct > 0 else 'bearish' if realized_return_pct < 0 else 'flat',
            realized_return_pct=round(realized_return_pct, 4),
            max_favorable_excursion_pct=(intraday or {}).get('favorable_move_pct_from_open'),
            max_adverse_excursion_pct=(intraday or {}).get('adverse_move_pct_from_open'),
            bullish_correct=bullish_correct,
            bearish_correct=bearish_correct,
            score=round(signed_score, 4),
            notes=_build_outcome_notes(
                source_label=source_label,
                symbol=symbol,
                entry_reference=float(entry_reference),
                evaluated_price=float(current_price),
                intraday=intraday,
            ),
        ),
        None,
    )


def evaluate_commodity_prediction(
    prediction: Dict[str, Any],
    evaluation_timestamp: datetime,
    evaluation_date: date,
    *,
    access_token: Optional[str],
    trend_threshold_pct: float = DEFAULT_TREND_THRESHOLD_PCT,
) -> tuple[BriefOutcomeRecord | None, Optional[str]]:
    symbol = prediction.get('symbol')
    entry_reference = prediction.get('entry_reference')
    direction = _normalize_predicted_direction(prediction.get('predicted_direction'))
    prediction_id = prediction.get('prediction_id')
    features = prediction.get('features') or {}
    instrument_key = features.get('instrument_key') or ((features.get('instrument') or {}).get('instrument_key'))

    if not symbol or not entry_reference or direction not in SUPPORTED_DIRECTIONS or not prediction_id:
        return None, 'missing prediction fields'
    if not instrument_key:
        return None, 'commodity instrument key unavailable'
    if not access_token:
        return None, 'upstox access token unavailable'

    day_context = _get_day_candle_context(access_token, instrument_key, evaluation_date)
    current_price = day_context.get('close') if day_context else None
    source_label = 'upstox_commodity_day_candle'
    if current_price is None:
        try:
            quote_payload = _fetch_quote_by_instrument_key(access_token, instrument_key)
        except Exception as exc:
            logger.warning('Could not fetch commodity quote for %s: %s', symbol, exc)
            return None, 'commodity quote unavailable'
        current_price = quote_payload.get('last_price')
        source_label = 'upstox_commodity_quote'
        if current_price is None:
            return None, 'evaluated price unavailable'

    realized_return_pct = ((float(current_price) - float(entry_reference)) / float(entry_reference)) * 100
    bullish_correct = realized_return_pct > 0
    bearish_correct = realized_return_pct < 0
    signed_score = realized_return_pct if direction == 'bullish' else -realized_return_pct
    intraday = (
        _compute_intraday_characteristics(
            predicted_direction=direction,
            day_context=day_context,
            trend_threshold_pct=trend_threshold_pct,
        )
        if day_context
        else None
    )

    return (
        BriefOutcomeRecord(
            outcome_id=f"outcome_{prediction_id}_{evaluation_date.isoformat()}",
            prediction_id=prediction_id,
            evaluation_timestamp=evaluation_timestamp.isoformat(),
            evaluation_date=evaluation_date.isoformat(),
            horizon_label=str(prediction.get('horizon_label') or 'SESSION'),
            realized_direction='bullish' if realized_return_pct > 0 else 'bearish' if realized_return_pct < 0 else 'flat',
            realized_return_pct=round(realized_return_pct, 4),
            max_favorable_excursion_pct=(intraday or {}).get('favorable_move_pct_from_open'),
            max_adverse_excursion_pct=(intraday or {}).get('adverse_move_pct_from_open'),
            bullish_correct=bullish_correct,
            bearish_correct=bearish_correct,
            score=round(signed_score, 4),
            notes=_build_outcome_notes(
                source_label=source_label,
                symbol=symbol,
                entry_reference=float(entry_reference),
                evaluated_price=float(current_price),
                intraday=intraday,
            ),
        ),
        None,
    )


def evaluate_macro_prediction(
    prediction: Dict[str, Any],
    evaluation_timestamp: datetime,
    evaluation_date: date,
    *,
    access_token: Optional[str],
    trend_threshold_pct: float = DEFAULT_TREND_THRESHOLD_PCT,
) -> tuple[BriefOutcomeRecord | None, Optional[str]]:
    prediction_id = prediction.get('prediction_id')
    direction = _normalize_predicted_direction(prediction.get('predicted_direction'))
    symbol = prediction.get('symbol') or 'NIFTY_CONTEXT'
    features = prediction.get('features') or {}
    instrument_key = features.get('instrument_key') or 'NSE_INDEX|Nifty 50'

    if direction not in SUPPORTED_DIRECTIONS or not prediction_id:
        return None, 'macro bias not directional'
    if not access_token:
        return None, 'upstox access token unavailable'

    day_context = _get_day_candle_context(access_token, instrument_key, evaluation_date)
    if not day_context:
        return None, 'macro context day candle unavailable'
    previous_close = day_context.get('previous_close')
    if previous_close is None:
        return None, 'macro context previous close unavailable'

    current_price = float(day_context['close'])
    entry_reference = float(previous_close)
    realized_return_pct = ((current_price - entry_reference) / entry_reference) * 100
    bullish_correct = realized_return_pct > 0
    bearish_correct = realized_return_pct < 0
    signed_score = realized_return_pct if direction == 'bullish' else -realized_return_pct
    intraday = _compute_intraday_characteristics(
        predicted_direction=direction,
        day_context=day_context,
        trend_threshold_pct=trend_threshold_pct,
    )

    return (
        BriefOutcomeRecord(
            outcome_id=f"outcome_{prediction_id}_{evaluation_date.isoformat()}",
            prediction_id=prediction_id,
            evaluation_timestamp=evaluation_timestamp.isoformat(),
            evaluation_date=evaluation_date.isoformat(),
            horizon_label=str(prediction.get('horizon_label') or 'OPEN'),
            realized_direction='bullish' if realized_return_pct > 0 else 'bearish' if realized_return_pct < 0 else 'flat',
            realized_return_pct=round(realized_return_pct, 4),
            max_favorable_excursion_pct=intraday.get('favorable_move_pct_from_open'),
            max_adverse_excursion_pct=intraday.get('adverse_move_pct_from_open'),
            bullish_correct=bullish_correct,
            bearish_correct=bearish_correct,
            score=round(signed_score, 4),
            notes=_build_outcome_notes(
                source_label='upstox_macro_nifty_context',
                symbol=symbol,
                entry_reference=entry_reference,
                evaluated_price=current_price,
                intraday=intraday,
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
    *,
    trend_threshold_pct: float,
) -> Dict[str, Any]:
    total = len(evaluated)
    correct = 0
    trend_counts = {
        'trended': 0,
        'sideways': 0,
        'moved_opposite': 0,
        'two_sided_volatile': 0,
        'unknown': 0,
    }
    for outcome in evaluated:
        prediction = prediction_map.get(outcome.prediction_id, {})
        if _is_directionally_correct(str(prediction.get('predicted_direction')), outcome):
            correct += 1
        character = 'unknown'
        if outcome.notes:
            for label in ('trended', 'sideways', 'moved_opposite', 'two_sided_volatile'):
                if f'Intraday character {label}' in outcome.notes:
                    character = label
                    break
        trend_counts[character] = trend_counts.get(character, 0) + 1

    summary = {
        'source_brief_run_id': source_run.get('brief_run_id'),
        'source_date': source_run.get('run_date'),
        'evaluated_count': total,
        'correct_count': correct,
        'hit_rate': round((correct / total) * 100, 2) if total else 0,
        'trend_threshold_pct': trend_threshold_pct,
        'intraday_character_counts': trend_counts,
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
                'max_favorable_excursion_pct': outcome.max_favorable_excursion_pct,
                'max_adverse_excursion_pct': outcome.max_adverse_excursion_pct,
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
    parser.add_argument(
        '--trend-threshold-pct',
        type=float,
        default=DEFAULT_TREND_THRESHOLD_PCT,
        help='Minimum move from the day open in the predicted direction to classify the day as intraday trending',
    )
    args = parser.parse_args()

    evaluation_timestamp = datetime.now()
    evaluation_date = datetime.strptime(args.source_date, '%Y-%m-%d').date()
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
        direction = _normalize_predicted_direction(prediction.get('predicted_direction'))
        if asset_class not in SUPPORTED_DIRECTIONAL_ASSET_CLASSES or direction not in SUPPORTED_DIRECTIONS:
            skipped.append(f"{prediction.get('symbol')} ({asset_class}/{direction})")
            continue

        if asset_class == 'macro':
            outcome, skip_reason = evaluate_macro_prediction(
                prediction,
                evaluation_timestamp,
                evaluation_date,
                access_token=access_token,
                trend_threshold_pct=args.trend_threshold_pct,
            )
        elif asset_class == 'index':
            outcome, skip_reason = evaluate_index_prediction(
                prediction,
                evaluation_timestamp,
                evaluation_date,
                access_token=access_token,
                trend_threshold_pct=args.trend_threshold_pct,
            )
        elif asset_class == 'commodity':
            outcome, skip_reason = evaluate_commodity_prediction(
                prediction,
                evaluation_timestamp,
                evaluation_date,
                access_token=access_token,
                trend_threshold_pct=args.trend_threshold_pct,
            )
        else:
            outcome, skip_reason = evaluate_equity_prediction(
                prediction,
                evaluation_timestamp,
                evaluation_date,
                provider=provider,
                access_token=access_token,
                trend_threshold_pct=args.trend_threshold_pct,
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
        _purge_existing_outcomes(list(prediction_map.keys()), evaluation_date.isoformat())
        archive_brief_outcomes(evaluated, DEFAULT_DB_PATH)

    summary = build_report(
        source_run,
        prediction_map,
        evaluated,
        skipped,
        text_path,
        trend_threshold_pct=args.trend_threshold_pct,
    )
    lines = [
        'BRIEF END-OF-DAY REVIEW',
        '=' * 80,
        f"Source Brief Run: {source_run.get('brief_run_id')} | {source_run.get('run_timestamp')}",
        f"Evaluated directional calls: {summary['evaluated_count']}",
        f"Correct calls: {summary['correct_count']} | Hit Rate: {summary['hit_rate']:.2f}%",
        (
            "Intraday character: "
            f"trended={summary['intraday_character_counts'].get('trended', 0)}, "
            f"sideways={summary['intraday_character_counts'].get('sideways', 0)}, "
            f"moved_opposite={summary['intraday_character_counts'].get('moved_opposite', 0)}, "
            f"two_sided_volatile={summary['intraday_character_counts'].get('two_sided_volatile', 0)} "
            f"(threshold {summary['trend_threshold_pct']:.2f}% from open)"
        ),
        '',
        'OUTCOMES',
        '-' * 80,
    ]

    for item in summary['outcomes']:
        character = 'unknown'
        notes = item.get('notes') or ''
        for label in ('trended', 'sideways', 'moved_opposite', 'two_sided_volatile'):
            if f'Intraday character {label}' in notes:
                character = label
                break
        lines.append(
            f"{item['prediction_id']}: {item['symbol']} | predicted {item['predicted_direction']} -> realized {item['realized_direction']} | "
            f"{'CORRECT' if item['is_correct'] else 'WRONG'} | return {item['realized_return_pct']:+.2f}% | "
            f"intraday {character} | mfe {float(item.get('max_favorable_excursion_pct') or 0):+.2f}% | "
            f"mae {float(item.get('max_adverse_excursion_pct') or 0):+.2f}% | score {item['score']:+.2f}"
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
