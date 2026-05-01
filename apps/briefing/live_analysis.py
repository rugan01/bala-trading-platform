#!/usr/bin/env python3
"""
Live Analysis - Intraday Thesis Check vs Morning Brief
=====================================================
Compares the latest live intraday market structure with the archived morning brief
and answers a practical question: is the morning thesis still intact, weakening,
or invalidated?
"""

import os
import sys
import json
import argparse
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(TOOLS_DIR))

REPO_ROOT = Path(__file__).resolve().parents[2]
TRADING_PLATFORM_ROOT = REPO_ROOT / 'packages' / 'trading_platform'
TRADING_PLATFORM_SRC = TRADING_PLATFORM_ROOT / 'src'
if TRADING_PLATFORM_SRC.exists():
    sys.path.insert(0, str(TRADING_PLATFORM_SRC))

from morning_brief import (
    DEFAULT_OUTPUT_DIR,
    SectionResult,
    classify_index_zone,
    infer_market_phase,
    run_fno_scanner,
    run_mcx_scanner,
    run_nifty_analysis,
)
from trading_platform.archive.bootstrap import DEFAULT_DB_PATH
from trading_platform.briefs import LiveAnalysisCheckRecord, LiveAnalysisRunRecord
from trading_platform.briefs.repository import archive_live_analysis, get_latest_brief_run, summarize_recent_learning

LOG_FILE = os.path.expanduser('~/Library/Logs/live_analysis.log')
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

DEFAULT_LIVE_OUTPUT_DIR = DEFAULT_OUTPUT_DIR / 'live'


def _serialize_section_result(result: SectionResult) -> Dict[str, Any]:
    return {
        'success': result.success,
        'text': result.text,
        'data': result.data,
    }


def _determine_index_status(predicted_bias: str, current_zone: str) -> str:
    if predicted_bias == 'bullish':
        if current_zone == 'above_r1':
            return 'strengthening'
        if current_zone == 'above_cpr':
            return 'intact'
        if current_zone == 'inside_cpr':
            return 'weakened'
        return 'invalidated'
    if predicted_bias == 'bearish':
        if current_zone == 'below_s1':
            return 'strengthening'
        if current_zone == 'below_cpr':
            return 'intact'
        if current_zone == 'inside_cpr':
            return 'weakened'
        return 'invalidated'
    if current_zone == 'inside_cpr':
        return 'intact'
    if current_zone in {'above_cpr', 'below_cpr'}:
        return 'mixed'
    return 'changed'


def compare_indices(morning_section: Dict[str, Any], live_result: SectionResult) -> List[LiveAnalysisCheckRecord]:
    morning_data = ((morning_section or {}).get('data') or {})
    live_data = live_result.data or {}
    if not morning_data or not live_data:
        return [
            LiveAnalysisCheckRecord(
                scope='index',
                symbol='NIFTY',
                thesis_status='no_comparison',
                summary_text='Index live comparison unavailable because morning or live structured data is missing.',
            )
        ]

    morning_indices = morning_data.get('index_snapshots') or {}
    live_indices = live_data.get('index_snapshots') or {}
    if not morning_indices or not live_indices:
        morning_indices = {
            'NIFTY': {
                'symbol': 'NIFTY',
                'spot': morning_data.get('spot'),
                'levels': morning_data.get('levels', {}),
                'bias': morning_data.get('bias', 'neutral'),
                'day_type': morning_data.get('day_type'),
            }
        }
        live_indices = {
            'NIFTY': {
                'symbol': 'NIFTY',
                'spot': live_data.get('spot'),
            }
        }

    checks: List[LiveAnalysisCheckRecord] = []
    vix_morning = float(((morning_data.get('vix') or {}).get('current') or 0))
    vix_live = float(((live_data.get('vix') or {}).get('current') or 0))
    vix_change = ((vix_live - vix_morning) / vix_morning * 100) if vix_morning else None

    for symbol in ('NIFTY', 'BANKNIFTY', 'SENSEX'):
        morning_snapshot = morning_indices.get(symbol)
        live_snapshot = live_indices.get(symbol)
        if not morning_snapshot or not live_snapshot:
            continue

        levels = morning_snapshot.get('levels', {})
        morning_spot = float(morning_snapshot.get('spot') or 0)
        current_spot = float(live_snapshot.get('spot') or 0)
        predicted_bias = str(morning_snapshot.get('bias') or 'neutral').lower()
        current_zone = classify_index_zone(current_spot, levels)
        delta_pct = ((current_spot - morning_spot) / morning_spot * 100) if morning_spot else None
        status = _determine_index_status(predicted_bias, current_zone)

        day_type = (morning_snapshot.get('day_type') or {}).get('label')
        if symbol == 'NIFTY' and day_type:
            summary = (
                f"Morning NIFTY thesis was {str(day_type).upper()} with {predicted_bias.upper()} bias; "
                f"current spot is {current_spot:,.2f} in zone {current_zone}. Status: {status}."
            )
        else:
            summary = (
                f"Morning {symbol} bias was {predicted_bias.upper()}; current spot is {current_spot:,.2f} "
                f"in zone {current_zone}. Status: {status}."
            )

        details = {
            'predicted_bias': predicted_bias,
            'current_zone': current_zone,
            'morning_levels': levels,
        }
        if symbol == 'NIFTY':
            details.update(
                {
                    'morning_day_type': str(day_type).upper() if day_type else None,
                    'morning_vix': vix_morning,
                    'live_vix': vix_live,
                    'vix_change_pct': round(vix_change, 3) if vix_change is not None else None,
                }
            )

        checks.append(
            LiveAnalysisCheckRecord(
                scope='index',
                symbol=symbol,
                thesis_status=status,
                current_price=current_spot,
                reference_price=morning_spot,
                delta_pct=round(delta_pct, 3) if delta_pct is not None else None,
                summary_text=summary,
                details=details,
            )
        )

    return checks


def _build_symbol_map(entries: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {entry.get('symbol'): entry for entry in entries if entry.get('symbol')}


def compare_fno(morning_section: Dict[str, Any], live_result: SectionResult, top_n: int) -> List[LiveAnalysisCheckRecord]:
    morning_data = ((morning_section or {}).get('data') or {})
    live_data = live_result.data or {}
    if not morning_data or not live_data:
        return [
            LiveAnalysisCheckRecord(
                scope='fno',
                thesis_status='no_comparison',
                summary_text='F&O live comparison unavailable because morning or live structured data is missing.',
            )
        ]

    live_bull_map = _build_symbol_map(live_data.get('bullish_stocks', []))
    live_bear_map = _build_symbol_map(live_data.get('bearish_stocks', []))
    live_skipped_map = _build_symbol_map(live_data.get('skipped_stocks', []))
    current_map = {**live_bull_map, **live_bear_map}
    checks: List[LiveAnalysisCheckRecord] = []

    for bucket, expected_direction in (('bullish_stocks', 'bullish'), ('bearish_stocks', 'bearish')):
        for rank, stock in enumerate(morning_data.get(bucket, [])[:top_n], 1):
            symbol = stock.get('symbol')
            current = current_map.get(symbol, {})
            skipped = live_skipped_map.get(symbol, {})
            if skipped:
                current_price = skipped.get('price')
                reference_price = stock.get('price')
                delta_pct = None
                if current_price and reference_price:
                    delta_pct = ((float(current_price) - float(reference_price)) / float(reference_price)) * 100
                checks.append(
                    LiveAnalysisCheckRecord(
                        scope='fno',
                        symbol=symbol,
                        thesis_status='no_comparison',
                        current_price=float(current_price) if current_price is not None else None,
                        reference_price=float(reference_price) if reference_price is not None else None,
                        delta_pct=round(delta_pct, 3) if delta_pct is not None else None,
                        summary_text=(
                            f"Rank #{rank} {expected_direction} pick {symbol} was skipped in live comparison "
                            f"because the price series looks discontinuous. {skipped.get('notes') or ''}".strip()
                        ),
                        details={
                            'expected_direction': expected_direction,
                            'morning_rank': rank,
                            'special_status': skipped.get('special_status'),
                            'notes': skipped.get('notes'),
                        },
                    )
                )
                continue

            current_price = current.get('price')
            reference_price = stock.get('price')
            delta_pct = None
            if current_price and reference_price:
                delta_pct = ((float(current_price) - float(reference_price)) / float(reference_price)) * 100

            if expected_direction == 'bullish':
                if symbol in live_bull_map:
                    status = 'intact'
                    summary = f'{symbol} remains in the live bullish basket.'
                elif symbol in live_bear_map:
                    status = 'invalidated'
                    summary = f'{symbol} flipped from morning bullish to live bearish.'
                else:
                    status = 'weakened'
                    summary = f'{symbol} dropped out of the live bullish basket.'
            else:
                if symbol in live_bear_map:
                    status = 'intact'
                    summary = f'{symbol} remains in the live bearish basket.'
                elif symbol in live_bull_map:
                    status = 'invalidated'
                    summary = f'{symbol} flipped from morning bearish to live bullish.'
                else:
                    status = 'weakened'
                    summary = f'{symbol} dropped out of the live bearish basket.'

            checks.append(
                LiveAnalysisCheckRecord(
                    scope='fno',
                    symbol=symbol,
                    thesis_status=status,
                    current_price=float(current_price) if current_price is not None else None,
                    reference_price=float(reference_price) if reference_price is not None else None,
                    delta_pct=round(delta_pct, 3) if delta_pct is not None else None,
                    summary_text=f'Rank #{rank} {expected_direction} pick {summary}',
                    details={
                        'expected_direction': expected_direction,
                        'morning_rank': rank,
                        'morning_score': stock.get('score'),
                        'live_score': current.get('score'),
                        'current_bucket': 'bullish' if symbol in live_bull_map else 'bearish' if symbol in live_bear_map else 'none',
                    },
                )
            )

    return checks


def compare_mcx(morning_section: Dict[str, Any], live_result: SectionResult, top_n: int) -> List[LiveAnalysisCheckRecord]:
    morning_data = ((morning_section or {}).get('data') or {})
    live_data = live_result.data or {}
    morning_setups = (morning_data.get('setups') or [])[:top_n]
    live_setups = _build_symbol_map(live_data.get('setups', [])) if live_data else {}

    if not morning_setups or not live_data:
        return [
            LiveAnalysisCheckRecord(
                scope='mcx',
                thesis_status='no_comparison',
                summary_text='MCX comparison skipped because morning or live MCX structured data is unavailable.',
            )
        ]

    checks: List[LiveAnalysisCheckRecord] = []
    for rank, setup in enumerate(morning_setups, 1):
        symbol = setup.get('symbol')
        live_setup = live_setups.get(symbol, {})
        morning_direction = setup.get('direction')
        live_direction = live_setup.get('direction')
        if morning_direction not in {'LONG', 'SHORT'}:
            continue

        if live_direction == morning_direction:
            status = 'intact'
            summary = f'{symbol} still supports the same MCX direction as the morning brief.'
        elif live_direction in {'LONG', 'SHORT'} and live_direction != morning_direction:
            status = 'invalidated'
            summary = f'{symbol} flipped direction versus the morning MCX setup.'
        else:
            status = 'weakened'
            summary = f'{symbol} no longer appears as a clean MCX setup in the live scan.'

        current_price = live_setup.get('ltp') or ((live_data.get('quotes') or {}).get(symbol) or {}).get('ltp')
        reference_price = setup.get('ltp')
        delta_pct = None
        if current_price and reference_price:
            delta_pct = ((float(current_price) - float(reference_price)) / float(reference_price)) * 100

        checks.append(
            LiveAnalysisCheckRecord(
                scope='mcx',
                symbol=symbol,
                thesis_status=status,
                current_price=float(current_price) if current_price is not None else None,
                reference_price=float(reference_price) if reference_price is not None else None,
                delta_pct=round(delta_pct, 3) if delta_pct is not None else None,
                summary_text=f'Rank #{rank} {summary}',
                details={
                    'morning_direction': morning_direction,
                    'live_direction': live_direction,
                    'morning_score': setup.get('score'),
                    'live_score': live_setup.get('score'),
                },
            )
        )

    return checks


def determine_overall_status(checks: List[LiveAnalysisCheckRecord]) -> str:
    statuses = [check.thesis_status for check in checks if check.thesis_status not in {'no_comparison'}]
    if not statuses:
        return 'no_comparison'
    if any(status == 'invalidated' for status in statuses):
        return 'changed'
    if any(status == 'weakened' for status in statuses):
        return 'mixed'
    if any(status == 'strengthening' for status in statuses):
        return 'strengthening'
    if all(status == 'intact' for status in statuses):
        return 'intact'
    return 'mixed'


def format_text_report(
    started_at: datetime,
    source_run: Dict[str, Any],
    learning_summary: str,
    checks: List[LiveAnalysisCheckRecord],
) -> str:
    lines: List[str] = []
    lines.append('╔' + '═' * 88 + '╗')
    lines.append('║' + 'LIVE ANALYSIS - THESIS CHECK VS MORNING BRIEF'.center(88) + '║')
    lines.append('║' + f"{started_at.strftime('%B %d, %Y %H:%M IST')}".center(88) + '║')
    lines.append('╚' + '═' * 88 + '╝')
    lines.append('')
    lines.append(f"Source Morning Brief Run: {source_run.get('brief_run_id')} | {source_run.get('run_timestamp')}")
    lines.append(f"Market Phase: {infer_market_phase(started_at)}")
    lines.append('')
    lines.append('LEARNING CONTEXT')
    lines.append('-' * 88)
    lines.append(learning_summary)
    lines.append('')
    lines.append('THESIS CHECKS')
    lines.append('-' * 88)

    for check in checks:
        symbol = check.symbol or check.scope.upper()
        lines.append(f"[{check.scope.upper()}] {symbol}: {check.thesis_status.upper()}")
        lines.append(f"  {check.summary_text}")
        if check.reference_price is not None and check.current_price is not None:
            delta = f"{check.delta_pct:+.2f}%" if check.delta_pct is not None else 'N/A'
            lines.append(
                f"  Reference: {check.reference_price:,.2f} | Current: {check.current_price:,.2f} | Delta: {delta}"
            )
        lines.append('')

    lines.append('NOTES')
    lines.append('-' * 88)
    lines.append('- F&O live comparison now uses Upstox live quotes and intraday session data for the scanned stock universe.')
    lines.append('- Index live comparison now covers NIFTY, BANKNIFTY, and SENSEX against the archived morning bias and CPR levels.')
    lines.append('- MCX comparison is most meaningful during the evening session when the scanner is active.')
    return '\n'.join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description='Live intraday thesis check vs morning brief')
    parser.add_argument('--output', type=str, default=str(DEFAULT_LIVE_OUTPUT_DIR), help='Directory to save the live analysis report')
    parser.add_argument('--source-date', type=str, default=datetime.now().date().isoformat(), help='Morning brief date to compare against')
    parser.add_argument('--sections', type=str, default='nifty,fno,mcx', help='Comma-separated sections to run (nifty,fno,mcx)')
    parser.add_argument('--top', type=int, default=5, help='Number of morning picks to compare for F&O/MCX')
    parser.add_argument('--no-archive', action='store_true', help='Skip archiving the live analysis run')
    args = parser.parse_args()

    started_at = datetime.now()
    logger.info('Starting live analysis at %s', started_at)

    source_run = get_latest_brief_run(DEFAULT_DB_PATH, run_date=args.source_date)
    if source_run is None:
        source_run = get_latest_brief_run(DEFAULT_DB_PATH)
        if source_run is None:
            logger.error('No archived morning brief run found.')
            return 1
        logger.warning('No morning brief found for %s. Falling back to latest archived brief %s', args.source_date, source_run.get('brief_run_id'))

    source_sections = (source_run.get('metadata') or {}).get('sections', {})
    sections_to_run = [section.strip().lower() for section in args.sections.split(',') if section.strip()]

    live_nifty = SectionResult(False, 'Skipped')
    live_fno = SectionResult(False, 'Skipped')
    live_mcx = SectionResult(False, 'Skipped')

    if 'nifty' in sections_to_run:
        live_nifty = run_nifty_analysis(mode='live')
    if 'fno' in sections_to_run:
        live_fno = run_fno_scanner(mode='live')
    if 'mcx' in sections_to_run:
        live_mcx = run_mcx_scanner()

    checks: List[LiveAnalysisCheckRecord] = []
    if 'nifty' in sections_to_run:
        checks.extend(compare_indices(source_sections.get('nifty', {}), live_nifty))
    if 'fno' in sections_to_run:
        checks.extend(compare_fno(source_sections.get('fno', {}), live_fno, args.top))
    if 'mcx' in sections_to_run:
        checks.extend(compare_mcx(source_sections.get('mcx', {}), live_mcx, args.top))

    learning_summary = summarize_recent_learning(DEFAULT_DB_PATH)
    overall_status = determine_overall_status(checks)
    text_report = format_text_report(started_at, source_run, learning_summary, checks)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)
    basename = f"live_analysis_{started_at.strftime('%Y%m%d_%H%M')}"
    text_path = output_dir / f'{basename}.txt'
    json_path = output_dir / f'{basename}.json'
    latest_text_path = output_dir / 'live_analysis_latest.txt'
    latest_json_path = output_dir / 'live_analysis_latest.json'

    payload = {
        'live_analysis_run_id': f"live_analysis_{started_at.strftime('%Y%m%d_%H%M%S')}",
        'generated_at': started_at.isoformat(),
        'market_phase': infer_market_phase(started_at),
        'source_brief_run_id': source_run.get('brief_run_id'),
        'source_brief_timestamp': source_run.get('run_timestamp'),
        'overall_status': overall_status,
        'learning_summary': learning_summary,
        'sections': {
            'nifty': _serialize_section_result(live_nifty),
            'fno': _serialize_section_result(live_fno),
            'mcx': _serialize_section_result(live_mcx),
        },
        'checks': [asdict(check) for check in checks],
        'report_path': str(text_path),
    }

    text_path.write_text(text_report, encoding='utf-8')
    latest_text_path.write_text(text_report, encoding='utf-8')
    json_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
    latest_json_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')

    if not args.no_archive:
        try:
            archive_live_analysis(
                LiveAnalysisRunRecord(
                    live_analysis_run_id=payload['live_analysis_run_id'],
                    source_brief_run_id=source_run.get('brief_run_id'),
                    run_timestamp=started_at.isoformat(),
                    run_date=started_at.date().isoformat(),
                    market_phase=infer_market_phase(started_at),
                    overall_status=overall_status,
                    summary_text=f'Live analysis status: {overall_status}',
                    metadata={
                        'report_path': str(text_path),
                        'json_path': str(json_path),
                        'source_brief_run_id': source_run.get('brief_run_id'),
                        'sections_run': sections_to_run,
                    },
                ),
                checks,
                DEFAULT_DB_PATH,
            )
        except Exception as exc:
            logger.warning('Failed to archive live analysis run: %s', exc)
            payload['archive_error'] = str(exc)
            json_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')
            latest_json_path.write_text(json.dumps(payload, indent=2), encoding='utf-8')

    print(text_report)
    logger.info('Live analysis saved to %s', text_path)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
