#!/usr/bin/env python3
"""
Morning Brief - Unified Pre-Market Analysis
============================================
Consolidates all pre-market analysis into a single comprehensive report and now
stores a structured JSON sidecar plus archive-backed predictions for learning.
"""

import os
import sys
import json
import argparse
import logging
import subprocess
from datetime import datetime, time, date, timedelta
from urllib.parse import quote

import requests
from pathlib import Path
from typing import Any, Dict, Optional
from dataclasses import dataclass, asdict
from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed

# Add Tools directory to path
TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(TOOLS_DIR))

# Add trading platform package to path when available
REPO_ROOT = Path(__file__).resolve().parents[2]
TRADING_PLATFORM_ROOT = REPO_ROOT / 'packages' / 'trading_platform'
TRADING_PLATFORM_SRC = TRADING_PLATFORM_ROOT / 'src'
if TRADING_PLATFORM_SRC.exists():
    sys.path.insert(0, str(TRADING_PLATFORM_SRC))

from dotenv import load_dotenv

try:
    from trading_platform.archive.bootstrap import DEFAULT_DB_PATH
    from trading_platform.briefs import BriefPredictionRecord, BriefRunRecord, archive_brief_run
    from trading_platform.briefs.repository import summarize_recent_learning
    from trading_platform.paths import ENV_FILE as PLATFORM_ENV_FILE, PREMARKET_REPORTS_ROOT
    PLATFORM_ARCHIVE_AVAILABLE = True
except Exception:
    DEFAULT_DB_PATH = None
    BriefPredictionRecord = None
    BriefRunRecord = None
    archive_brief_run = None
    summarize_recent_learning = None
    PLATFORM_ENV_FILE = REPO_ROOT / '.env'
    PREMARKET_REPORTS_ROOT = REPO_ROOT / 'data' / 'reports' / 'premarket'
    PLATFORM_ARCHIVE_AVAILABLE = False

# Configure logging
LOG_FILE = os.path.expanduser('~/Library/Logs/morning_brief.log')
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

# Default output directory
DEFAULT_OUTPUT_DIR = PREMARKET_REPORTS_ROOT


@dataclass(slots=True)
class SectionResult:
    success: bool
    text: str
    data: Optional[Dict[str, Any]] = None


def infer_market_phase(now: Optional[datetime] = None) -> str:
    current = now or datetime.now()
    local_time = current.time()

    if local_time < time(9, 15):
        return 'premarket'
    if local_time <= time(15, 30):
        return 'intraday'
    if local_time < time(17, 0):
        return 'postclose'
    if local_time <= time(23, 30):
        return 'evening_session'
    return 'offhours'


def _serialize_section_result(result: SectionResult) -> Dict[str, Any]:
    return {
        'success': result.success,
        'text': result.text,
        'data': result.data,
    }


def _normalize_score(value: float, divisor: float, floor: float = 0.3, ceiling: float = 0.95) -> float:
    if divisor <= 0:
        return floor
    normalized = abs(value) / divisor
    return round(max(floor, min(ceiling, normalized)), 4)


def _build_learning_summary() -> str:
    if not PLATFORM_ARCHIVE_AVAILABLE or summarize_recent_learning is None:
        return 'Learning loop not yet available because the trading platform archive package could not be imported.'

    try:
        return summarize_recent_learning(DEFAULT_DB_PATH)
    except Exception as exc:
        logger.warning('Failed to build learning summary: %s', exc)
        return f'Learning summary unavailable for this run: {exc}'


def _load_upstox_token(env_file: Optional[Path] = None) -> str:
    load_dotenv(env_file or PLATFORM_ENV_FILE)
    return os.getenv('UPSTOX_ACCESS_TOKEN', '').strip("'\"")


INDEX_BRIEF_CONFIG: Dict[str, Dict[str, str]] = {
    'NIFTY': {
        'display_name': 'NIFTY 50',
        'instrument_key': 'NSE_INDEX|Nifty 50',
        'universe': 'NSE_INDEX',
    },
    'BANKNIFTY': {
        'display_name': 'NIFTY BANK',
        'instrument_key': 'NSE_INDEX|Nifty Bank',
        'universe': 'NSE_INDEX',
    },
    'SENSEX': {
        'display_name': 'SENSEX',
        'instrument_key': 'BSE_INDEX|SENSEX',
        'universe': 'BSE_INDEX',
    },
}


def _fetch_upstox_quote(token: str, instrument_key: str) -> dict:
    response = requests.get(
        'https://api.upstox.com/v2/market-quote/quotes',
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}',
        },
        params={'instrument_key': instrument_key},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json().get('data', {})
    return next(iter(payload.values()), {})


def _fetch_upstox_daily_history(token: str, instrument_key: str, days: int = 6) -> list[dict]:
    to_date = date.today()
    from_date = to_date - timedelta(days=max(days * 3, 20))
    url = f"https://api.upstox.com/v2/historical-candle/{quote(instrument_key, safe='')}/day/{to_date.isoformat()}/{from_date.isoformat()}"
    response = requests.get(
        url,
        headers={
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {token}',
        },
        timeout=20,
    )
    response.raise_for_status()
    candles = response.json().get('data', {}).get('candles', [])
    parsed = [
        {
            'date': candle[0],
            'open': float(candle[1]),
            'high': float(candle[2]),
            'low': float(candle[3]),
            'close': float(candle[4]),
            'volume': int(candle[5] or 0),
        }
        for candle in reversed(candles)
    ]
    return parsed[-days:]


def _extract_candle_date(value: Any) -> Optional[date]:
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


def _get_closed_daily_history(history: list[dict], *, min_length: int) -> list[dict]:
    if not history:
        return []

    trimmed = list(history)
    today = date.today()
    latest_date = _extract_candle_date(trimmed[-1].get('date'))
    if latest_date == today and len(trimmed) > min_length:
        trimmed = trimmed[:-1]
    return trimmed


def _build_vix_eod_payload(vix_history: list[dict]) -> dict:
    latest = vix_history[-1]
    previous = vix_history[-2] if len(vix_history) > 1 else latest
    return {
        'last_price': latest['close'],
        'ohlc': {
            'open': latest['open'],
            'high': latest['high'],
            'low': latest['low'],
            'close': previous['close'],
        },
    }


def classify_index_zone(spot: float, levels: Dict[str, Any]) -> str:
    r1 = float(levels.get('r1', 0) or 0)
    s1 = float(levels.get('s1', 0) or 0)
    tc = float(levels.get('tc', 0) or 0)
    bc = float(levels.get('bc', 0) or 0)

    if spot >= r1 and r1 > 0:
        return 'above_r1'
    if tc > 0 and spot > tc:
        return 'above_cpr'
    if bc > 0 and tc > 0 and bc <= spot <= tc:
        return 'inside_cpr'
    if s1 > 0 and spot <= s1:
        return 'below_s1'
    return 'below_cpr'


def _derive_index_bias(spot: float, levels: Any) -> tuple[str, str, str]:
    levels_dict = asdict(levels) if hasattr(levels, '__dataclass_fields__') else dict(levels)
    zone = classify_index_zone(spot, levels_dict)
    if spot > float(levels_dict.get('tc', 0) or 0):
        return 'bullish', zone, 'ABOVE CPR / PIVOT (Bullish)'
    if spot < float(levels_dict.get('bc', 0) or 0):
        return 'bearish', zone, 'BELOW CPR / PIVOT (Bearish)'
    return 'neutral', zone, 'INSIDE CPR (Neutral)'


def _build_index_snapshot(
    token: str,
    symbol: str,
    *,
    mode: str,
    vix: Any = None,
) -> dict:
    from premarket_analysis import NiftyLevels, predict_day_type

    config = INDEX_BRIEF_CONFIG[symbol]
    raw_history = _fetch_upstox_daily_history(token, config['instrument_key'], days=6)
    closed_history = _get_closed_daily_history(raw_history, min_length=2)
    if len(closed_history) < 2:
        raise ValueError(f'Could not fetch sufficient historical data for {symbol}')

    latest_closed = closed_history[-1]
    previous_closed = closed_history[-2]

    if mode == 'live':
        quote_payload = _fetch_upstox_quote(token, config['instrument_key'])
        spot = float(quote_payload.get('last_price') or latest_closed['close'])
        gap_pct = ((spot - latest_closed['close']) / latest_closed['close']) * 100 if latest_closed['close'] else 0
        change_pct = gap_pct
    else:
        quote_payload = {
            'last_price': latest_closed['close'],
            'ohlc': {
                'open': latest_closed['open'],
                'high': latest_closed['high'],
                'low': latest_closed['low'],
                'close': previous_closed['close'],
            },
            'volume': latest_closed['volume'],
            'net_change': latest_closed['close'] - previous_closed['close'],
        }
        spot = float(latest_closed['close'])
        gap_pct = 0
        change_pct = ((latest_closed['close'] - previous_closed['close']) / previous_closed['close']) * 100 if previous_closed['close'] else 0

    levels = NiftyLevels.calculate(
        high=latest_closed['high'],
        low=latest_closed['low'],
        close=latest_closed['close'],
        spot=spot,
    )
    bias, zone, position_label = _derive_index_bias(spot, levels)
    day_type_label = None
    day_type_score = None
    if vix is not None:
        day_type_label, day_type_score = predict_day_type(levels, vix, gap_pct)

    return {
        'symbol': symbol,
        'display_name': config['display_name'],
        'universe': config['universe'],
        'instrument_key': config['instrument_key'],
        'spot': round(spot, 2),
        'reference_close': round(float(latest_closed['close']), 2),
        'previous_reference_close': round(float(previous_closed['close']), 2),
        'gap_pct': round(gap_pct, 3),
        'change_pct': round(change_pct, 3),
        'bias': bias,
        'zone': zone,
        'position_label': position_label,
        'levels': asdict(levels),
        'day_type': (
            {'label': day_type_label, 'score': day_type_score}
            if day_type_label is not None
            else None
        ),
        'historical': closed_history,
        'quote': quote_payload,
        'data_mode': mode,
    }


def _append_other_indices_report(
    report_text: str,
    index_snapshots: Dict[str, Dict[str, Any]],
    *,
    unavailable: Optional[list[str]] = None,
) -> str:
    lines = [report_text, '', 'ADDITIONAL INDEX SNAPSHOTS', '-' * 40]
    for symbol in ('BANKNIFTY', 'SENSEX'):
        snapshot = index_snapshots.get(symbol)
        if not snapshot:
            continue
        levels = snapshot.get('levels', {})
        day_type = snapshot.get('day_type') or {}
        lines.append(f"{snapshot.get('display_name', symbol)}")
        lines.append(
            f"Spot: {snapshot.get('spot', 0):,.2f} | Bias: {str(snapshot.get('bias', 'neutral')).upper()} | "
            f"Zone: {snapshot.get('zone', 'unknown')}"
        )
        lines.append(
            f"PDH: {levels.get('prev_high', 0):,.2f} | PDL: {levels.get('prev_low', 0):,.2f} | "
            f"Prev Close: {levels.get('prev_close', 0):,.2f}"
        )
        lines.append(
            f"Pivot: {levels.get('pivot', 0):,.2f} | CPR: {levels.get('bc', 0):,.2f} - {levels.get('tc', 0):,.2f} | "
            f"R1/S1: {levels.get('r1', 0):,.2f} / {levels.get('s1', 0):,.2f}"
        )
        if day_type.get('label'):
            lines.append(f"Day Type Proxy: {day_type.get('label')} ({day_type.get('score')}% confidence)")
        lines.append('')

    if unavailable:
        lines.append('Unavailable: ' + ', '.join(unavailable))

    return '\n'.join(lines).rstrip()


# =============================================================================
# SECTION RUNNERS
# =============================================================================

def run_global_markets() -> SectionResult:
    logger.info('Running Global Markets analysis...')
    try:
        from global_markets import fetch_all_markets, format_report

        report = fetch_all_markets()
        formatted = format_report(report)
        return SectionResult(True, formatted, asdict(report))
    except Exception as e:
        logger.error(f'Global Markets failed: {e}')
        import traceback
        traceback.print_exc()
        return SectionResult(False, f'Global Markets analysis failed: {e}')


def run_nifty_analysis(*, mode: str = 'eod') -> SectionResult:
    logger.info('Running Index analysis in %s mode...', mode)
    try:
        token = _load_upstox_token()
        if not token:
            return SectionResult(False, 'UPSTOX_ACCESS_TOKEN not found')

        from premarket_analysis import (
            UpstoxClient, NiftyLevels, analyze_vix, get_nearest_expiry,
            analyze_option_chain, predict_day_type, generate_strike_recommendations,
            generate_report
        )

        upstox = UpstoxClient(token)
        if mode == 'live':
            vix_payload = upstox.get_india_vix()
            expiries = upstox.get_weekly_expiries()
            nearest_expiry = get_nearest_expiry(expiries)
        else:
            raw_vix_history = _fetch_upstox_daily_history(token, 'NSE_INDEX|India VIX', days=3)
            vix_history = _get_closed_daily_history(raw_vix_history, min_length=2)
            if len(vix_history) < 2:
                return SectionResult(False, 'Could not fetch sufficient India VIX historical data')
            vix_payload = _build_vix_eod_payload(vix_history)
            nearest_expiry = 'EOD_ONLY'

        vix = analyze_vix(vix_payload)
        index_snapshots: Dict[str, Dict[str, Any]] = {}
        unavailable_indices: list[str] = []
        for symbol in ('NIFTY', 'BANKNIFTY', 'SENSEX'):
            try:
                index_snapshots[symbol] = _build_index_snapshot(token, symbol, mode=mode, vix=vix)
            except Exception as exc:
                logger.warning('Could not build %s snapshot: %s', symbol, exc)
                if symbol == 'NIFTY':
                    raise
                unavailable_indices.append(symbol)

        nifty_snapshot = index_snapshots['NIFTY']
        levels = NiftyLevels(**nifty_snapshot['levels'])
        spot = float(nifty_snapshot['spot'])
        gap_pct = float(nifty_snapshot['gap_pct'])
        nifty_quote = nifty_snapshot['quote']
        day_type = nifty_snapshot.get('day_type') or {}
        day_type_label = str(day_type.get('label') or 'UNCERTAIN')
        day_type_score = int(day_type.get('score') or 50)
        options: list[Any] = []
        if mode == 'live' and nearest_expiry:
            chain_data = upstox.get_option_chain(nearest_expiry)
            options = analyze_option_chain(chain_data, spot)
        recommendations = generate_strike_recommendations(spot, options) if options else {'bullish': {}, 'bearish': {}, 'neutral': {}}

        report = generate_report(
            levels,
            vix,
            options,
            nearest_expiry or 'N/A',
            (day_type_label, day_type_score),
            recommendations,
        )
        report = _append_other_indices_report(report, index_snapshots, unavailable=unavailable_indices)

        structured = {
            'spot': spot,
            'previous_reference_close': nifty_snapshot['previous_reference_close'],
            'gap_pct': round(gap_pct, 3),
            'bias': nifty_snapshot['bias'],
            'zone': nifty_snapshot['zone'],
            'position_label': nifty_snapshot['position_label'],
            'nearest_expiry': nearest_expiry,
            'levels': nifty_snapshot['levels'],
            'vix': asdict(vix),
            'day_type': {'label': day_type_label, 'score': day_type_score},
            'recommendations': recommendations,
            'options': [asdict(option) for option in options],
            'historical': nifty_snapshot['historical'],
            'quote': nifty_quote,
            'index_snapshots': index_snapshots,
            'unavailable_indices': unavailable_indices,
            'data_mode': mode,
        }
        return SectionResult(True, report, structured)

    except Exception as e:
        logger.error(f'Nifty analysis failed: {e}')
        import traceback
        traceback.print_exc()
        return SectionResult(False, f'Nifty analysis failed: {e}')


def run_fno_scanner(*, mode: str = 'eod') -> SectionResult:
    logger.info('Running F&O Scanner in %s mode...', mode)
    try:
        from fno_scanner import (
            SECTOR_INDICES,
            get_upstox_provider,
            fetch_market_data,
            analyze_sectors,
            scan_stocks,
            generate_report,
            format_text_report,
        )

        provider = get_upstox_provider()
        nifty_config = SECTOR_INDICES['NIFTY 50']
        nifty_data = fetch_market_data(
            'NIFTY 50',
            '1y',
            source='upstox',
            mode=mode,
            provider=provider,
            kind='index',
            aliases=nifty_config.get('upstox_aliases'),
            yahoo_symbol=nifty_config.get('yahoo_symbol'),
        )
        if not nifty_data:
            return SectionResult(False, 'Failed to fetch Nifty data for F&O scanner')

        sectors = analyze_sectors(nifty_data, source='upstox', mode=mode, provider=provider)
        bullish, bearish, skipped = scan_stocks(nifty_data, sectors, max_workers=5, source='upstox', mode=mode, provider=provider)
        report = generate_report(nifty_data, sectors, bullish, bearish, skipped, top_n=10, data_source='upstox', data_mode=mode)
        text_report = format_text_report(report)

        structured = {
            'timestamp': report.timestamp,
            'nifty_price': report.nifty_price,
            'nifty_change': report.nifty_change,
            'strong_sectors': report.strong_sectors,
            'weak_sectors': report.weak_sectors,
            'data_source': report.data_source,
            'data_mode': report.data_mode,
            'sectors': [asdict(s) for s in report.sectors],
            'bullish_stocks': [asdict(s) for s in report.bullish_stocks],
            'bearish_stocks': [asdict(s) for s in report.bearish_stocks],
            'skipped_stocks': [asdict(s) for s in report.skipped_stocks],
        }
        return SectionResult(True, text_report, structured)

    except Exception as e:
        logger.error(f'F&O Scanner failed: {e}')
        import traceback
        traceback.print_exc()
        return SectionResult(False, f'F&O Scanner failed: {e}')


def _run_mcx_scanner_output(output_format: str) -> Any:
    mcx_scanner_path = TOOLS_DIR / 'mcx_scanner' / 'mcx_scanner.py'
    result = subprocess.run(
        [sys.executable, str(mcx_scanner_path), '--output', output_format],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, 'PYTHONPATH': str(TOOLS_DIR)}
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or 'MCX scanner failed')

    if output_format == 'json':
        raw = result.stdout.strip()
        json_start = raw.find('{')
        if json_start == -1:
            raise ValueError('Could not locate JSON payload in MCX scanner output')
        return json.loads(raw[json_start:])
    return result.stdout


def run_mcx_scanner() -> SectionResult:
    logger.info('Running MCX Scanner...')
    try:
        mcx_scanner_path = TOOLS_DIR / 'mcx_scanner' / 'mcx_scanner.py'
        if not mcx_scanner_path.exists():
            return SectionResult(False, 'MCX Scanner not found')

        text_output = _run_mcx_scanner_output('text')
        structured_output = None
        try:
            structured_output = _run_mcx_scanner_output('json')
        except Exception as exc:
            logger.warning('MCX structured payload unavailable for this run: %s', exc)

        return SectionResult(True, text_output, structured_output)

    except subprocess.TimeoutExpired:
        return SectionResult(False, 'MCX Scanner timed out')
    except Exception as e:
        logger.error(f'MCX Scanner failed: {e}')
        return SectionResult(False, f'MCX Scanner failed: {e}')


# =============================================================================
# REPORT CONSOLIDATION
# =============================================================================

def create_header(now: datetime) -> str:
    header = []
    header.append('╔' + '═' * 88 + '╗')
    header.append('║' + ' ' * 88 + '║')
    header.append('║' + 'MORNING BRIEF - PRE-MARKET ANALYSIS'.center(88) + '║')
    header.append('║' + f"{now.strftime('%B %d, %Y (%A) %H:%M IST')}".center(88) + '║')
    header.append('║' + ' ' * 88 + '║')
    header.append('╚' + '═' * 88 + '╝')
    return '\n'.join(header)


def create_section_header(title: str) -> str:
    lines = []
    lines.append('')
    lines.append('')
    lines.append('┌' + '─' * 88 + '┐')
    lines.append('│' + f'  {title}'.ljust(88) + '│')
    lines.append('└' + '─' * 88 + '┘')
    return '\n'.join(lines)


def create_footer(stats: Dict[str, Dict[str, Any]]) -> str:
    lines = []
    lines.append('')
    lines.append('╔' + '═' * 88 + '╗')
    lines.append('║' + 'EXECUTION SUMMARY'.center(88) + '║')
    lines.append('╠' + '═' * 88 + '╣')

    for section, status in stats.items():
        status_icon = '✓' if status['success'] else '✗'
        time_str = f"{status['time']:.1f}s" if status['time'] else 'N/A'
        line = f"  {status_icon} {section:<30} {time_str:>10}"
        lines.append('║' + line.ljust(88) + '║')

    lines.append('╚' + '═' * 88 + '╝')
    lines.append('')
    lines.append("Generated by Morning Brief | Bala's Product OS")

    return '\n'.join(lines)


def build_quick_summary_lines(
    global_result: SectionResult,
    nifty_result: SectionResult,
    fno_result: SectionResult,
    mcx_result: SectionResult,
) -> list[str]:
    summary_lines: list[str] = []

    if global_result.success and global_result.data:
        bias = (global_result.data.get('overall_bias') or 'NEUTRAL').upper()
        summary_lines.append(f'  GLOBAL: Overnight markets {bias}')

    if nifty_result.success and nifty_result.data:
        day_type = nifty_result.data.get('day_type', {}).get('label')
        if day_type:
            summary_lines.append(f'  NIFTY: Expecting {day_type} day')
        index_snapshots = nifty_result.data.get('index_snapshots', {})
        index_parts = []
        for symbol in ('NIFTY', 'BANKNIFTY', 'SENSEX'):
            snapshot = index_snapshots.get(symbol)
            if snapshot:
                index_parts.append(f"{symbol} {str(snapshot.get('bias', 'neutral')).upper()}")
        if index_parts:
            summary_lines.append('  INDEX BIAS: ' + ' | '.join(index_parts))

    if fno_result.success and fno_result.data:
        bullish_count = len(fno_result.data.get('bullish_stocks', []))
        bearish_count = len(fno_result.data.get('bearish_stocks', []))
        summary_lines.append(f'  F&O: {bullish_count} bullish setups, {bearish_count} bearish setups')

    if mcx_result.success and mcx_result.data:
        setups = mcx_result.data.get('setups', [])
        long_count = sum(1 for setup in setups if setup.get('direction') == 'LONG')
        short_count = sum(1 for setup in setups if setup.get('direction') == 'SHORT')
        summary_lines.append(f'  MCX: {long_count} long setups, {short_count} short setups')

    return summary_lines


def consolidate_reports(
    now: datetime,
    global_result: SectionResult,
    nifty_result: SectionResult,
    fno_result: SectionResult,
    mcx_result: SectionResult,
    run_times: Dict[str, float],
    learning_summary: str,
    sections_to_run: list[str],
) -> str:
    sections = []
    sections.append(create_header(now))

    sections.append(create_section_header('QUICK SUMMARY'))
    sections.append('')
    summary_lines = build_quick_summary_lines(global_result, nifty_result, fno_result, mcx_result)
    sections.append('\n'.join(summary_lines) if summary_lines else '  Run individual sections for summary')

    sections.append(create_section_header('LEARNING LOOP (Recent Feedback)'))
    sections.append('')
    sections.append(learning_summary)

    ordered_sections = [
        ('global', 'GLOBAL MARKETS (Overnight Action)', global_result, 'Global Markets'),
        ('nifty', 'INDEX ANALYSIS (Nifty, BankNifty, Sensex)', nifty_result, 'Index Analysis'),
        ('fno', 'F&O STOCK SCANNER (Bullish & Bearish Picks)', fno_result, 'F&O Scanner'),
        ('mcx', 'MCX COMMODITIES (Evening Session Setup)', mcx_result, 'MCX Scanner'),
    ]

    stats: Dict[str, Dict[str, Any]] = {}
    section_number = 1
    for key, title, result, footer_label in ordered_sections:
        if key not in sections_to_run:
            continue
        sections.append(create_section_header(f'{section_number}. {title}'))
        sections.append(result.text if result.success else f"\n  ⚠ {result.text}")
        stats[footer_label] = {'success': result.success, 'time': run_times.get(key, 0)}
        section_number += 1

    sections.append(create_footer(stats))

    return '\n'.join(sections)


# =============================================================================
# ARCHIVE HELPERS
# =============================================================================

def build_brief_predictions(
    global_result: SectionResult,
    nifty_result: SectionResult,
    fno_result: SectionResult,
    mcx_result: SectionResult,
) -> list[Any]:
    if not PLATFORM_ARCHIVE_AVAILABLE or BriefPredictionRecord is None:
        return []

    predictions: list[Any] = []

    if global_result.success and global_result.data:
        bias = str(global_result.data.get('overall_bias') or 'NEUTRAL').lower().replace(' ', '_')
        predictions.append(
            BriefPredictionRecord(
                asset_class='macro',
                universe='GLOBAL',
                symbol='NIFTY_CONTEXT',
                timeframe='overnight',
                horizon_label='OPEN',
                signal_family='overnight_risk_sentiment',
                predicted_direction=bias,
                confidence_score=0.6,
                regime_label=global_result.data.get('risk_sentiment'),
                recommendation_text=f"Overnight markets bias for Nifty: {global_result.data.get('overall_bias', 'NEUTRAL')}",
                features=global_result.data,
                metadata={'section': 'global'},
            )
        )

    if nifty_result.success and nifty_result.data:
        levels = nifty_result.data.get('levels', {})
        vix_data = nifty_result.data.get('vix', {})
        day_type = nifty_result.data.get('day_type', {})
        predictions.append(
            BriefPredictionRecord(
                asset_class='index',
                universe='NSE_INDEX',
                symbol='NIFTY',
                timeframe='intraday',
                horizon_label='EOD',
                signal_family='market_regime',
                predicted_direction=str(day_type.get('label', 'UNCERTAIN')).lower().replace(' ', '_'),
                confidence_score=round((day_type.get('score', 50) or 50) / 100.0, 4),
                regime_label=vix_data.get('status'),
                recommendation_text=(
                    f"Morning thesis: {day_type.get('label', 'UNCERTAIN')} day. "
                    f"Spot {nifty_result.data.get('spot', 0):,.2f}, pivot {levels.get('pivot', 0):,.2f}, VIX {vix_data.get('current', 0):.2f}."
                ),
                entry_reference=nifty_result.data.get('spot'),
                features=nifty_result.data,
                metadata={'section': 'nifty'},
            )
        )

        for symbol in ('NIFTY', 'BANKNIFTY', 'SENSEX'):
            snapshot = (nifty_result.data.get('index_snapshots') or {}).get(symbol)
            if not snapshot:
                continue
            bias = str(snapshot.get('bias') or 'neutral').lower()
            if bias not in ('bullish', 'bearish'):
                continue
            levels_data = snapshot.get('levels', {})
            confidence = _normalize_score(
                float(abs(snapshot.get('gap_pct', 0))) + float(abs(snapshot.get('change_pct', 0))) + 4,
                10.0,
            )
            predictions.append(
                BriefPredictionRecord(
                    asset_class='index',
                    universe=snapshot.get('universe', 'INDEX'),
                    symbol=symbol,
                    timeframe='intraday',
                    horizon_label='EOD',
                    signal_family='index_cpr_bias',
                    predicted_direction=bias,
                    confidence_score=confidence,
                    regime_label=(snapshot.get('day_type') or {}).get('label') or snapshot.get('zone'),
                    recommendation_text=(
                        f"{symbol} morning bias {bias.upper()} from spot {snapshot.get('spot', 0):,.2f}. "
                        f"Pivot {levels_data.get('pivot', 0):,.2f}, CPR zone {snapshot.get('zone', 'unknown')}."
                    ),
                    entry_reference=snapshot.get('spot'),
                    features=snapshot,
                    metadata={'section': 'nifty', 'prediction_type': 'directional_index_bias'},
                )
            )

    if fno_result.success and fno_result.data:
        for bucket, direction in (('bullish_stocks', 'bullish'), ('bearish_stocks', 'bearish')):
            stocks = fno_result.data.get(bucket, [])
            for rank, stock in enumerate(stocks, 1):
                confidence = _normalize_score(float(stock.get('score', 0)), 10.0)
                recommendation_text = (
                    f"{direction.title()} setup rank #{rank}: {stock.get('symbol')} at {stock.get('price', 0):,.2f}. "
                    f"RSI {stock.get('rsi_14', 0):.1f}, RS vs Nifty {stock.get('rs_vs_nifty', 0):+,.1f}."
                )
                predictions.append(
                    BriefPredictionRecord(
                        asset_class='equity',
                        universe='NSE_FNO',
                        symbol=stock.get('symbol', 'UNKNOWN'),
                        timeframe='intraday',
                        horizon_label='EOD',
                        signal_family='relative_strength_trend',
                        predicted_direction=direction,
                        confidence_score=confidence,
                        expected_move_pct=stock.get('change_1d'),
                        setup_quality=confidence,
                        regime_label=stock.get('sector'),
                        recommendation_text=recommendation_text,
                        entry_reference=stock.get('price'),
                        features=stock,
                        metadata={'section': 'fno', 'bucket': bucket, 'rank': rank},
                    )
                )

    if mcx_result.success and mcx_result.data:
        instrument_map = mcx_result.data.get('instruments', {}) or {}
        quote_map = mcx_result.data.get('quotes', {}) or {}
        for rank, setup in enumerate(mcx_result.data.get('setups', []), 1):
            direction = setup.get('direction')
            if direction not in ('LONG', 'SHORT'):
                continue
            symbol = setup.get('symbol', 'UNKNOWN')
            enriched_features = {
                **setup,
                'instrument': instrument_map.get(symbol, {}),
                'quote': quote_map.get(symbol, {}),
                'instrument_key': (instrument_map.get(symbol, {}) or {}).get('instrument_key'),
            }
            predictions.append(
                BriefPredictionRecord(
                    asset_class='commodity',
                    universe='MCX',
                    symbol=symbol,
                    timeframe='intraday',
                    horizon_label='SESSION',
                    signal_family='mcx_intraday_setup',
                    predicted_direction='bullish' if direction == 'LONG' else 'bearish',
                    confidence_score=_normalize_score(float(setup.get('score', 0)), 13.0),
                    setup_quality=_normalize_score(float(setup.get('score', 0)), 13.0),
                    regime_label=setup.get('name'),
                    recommendation_text=(
                        f"{direction} setup rank #{rank}: entry {setup.get('entry_low')} - {setup.get('entry_high')}, "
                        f"SL {setup.get('stop_loss')}, T1 {setup.get('target_1')}, T2 {setup.get('target_2')}"
                    ),
                    entry_reference=setup.get('ltp'),
                    stop_reference=setup.get('stop_loss'),
                    target_reference=setup.get('target_1'),
                    features=enriched_features,
                    metadata={'section': 'mcx', 'rank': rank},
                )
            )

    return predictions


def send_notification(title: str, message: str, success: bool = True):
    try:
        sound = 'Glass' if success else 'Basso'
        script = f'display notification "{message}" with title "{title}" sound name "{sound}"'
        subprocess.run(['osascript', '-e', script], capture_output=True)
    except Exception as e:
        logger.warning(f'Failed to send notification: {e}')


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Morning Brief - Unified Pre-Market Analysis')
    parser.add_argument('--output', type=str, default=str(DEFAULT_OUTPUT_DIR), help='Directory to save report')
    parser.add_argument('--sections', type=str, help='Comma-separated list of sections to run (global,nifty,fno,mcx)')
    parser.add_argument('--notify', action='store_true', help='Send macOS notification when done')
    parser.add_argument('--parallel', action='store_true', default=True, help='Run sections in parallel (default: true)')
    parser.add_argument('--mode', choices=['manual', 'scheduled', 'replay'], default='manual', help='Run mode for archive tracking')
    parser.add_argument('--no-archive', action='store_true', help='Skip writing structured brief data into the platform archive')
    args = parser.parse_args()

    started_at = datetime.now()
    logger.info('=' * 60)
    logger.info('Morning Brief Started at %s', started_at)
    logger.info('=' * 60)

    if args.sections:
        sections_to_run = [s.strip().lower() for s in args.sections.split(',')]
    else:
        sections_to_run = ['global', 'nifty', 'fno', 'mcx']

    global_result = SectionResult(False, 'Skipped')
    nifty_result = SectionResult(False, 'Skipped')
    fno_result = SectionResult(False, 'Skipped')
    mcx_result = SectionResult(False, 'Skipped')
    run_times: Dict[str, float] = {}

    import time as timer

    if args.parallel:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {}
            if 'global' in sections_to_run:
                futures['global'] = (timer.time(), executor.submit(run_global_markets))
            if 'nifty' in sections_to_run:
                futures['nifty'] = (timer.time(), executor.submit(run_nifty_analysis, mode='eod'))
            if 'fno' in sections_to_run:
                futures['fno'] = (timer.time(), executor.submit(run_fno_scanner, mode='eod'))
            if 'mcx' in sections_to_run:
                futures['mcx'] = (timer.time(), executor.submit(run_mcx_scanner))

            future_map = {future: (name, started) for name, (started, future) in futures.items()}
            for future in as_completed(future_map):
                name, started = future_map[future]
                try:
                    result = future.result(timeout=300)
                    run_times[name] = timer.time() - started
                    if name == 'global':
                        global_result = result
                    elif name == 'nifty':
                        nifty_result = result
                    elif name == 'fno':
                        fno_result = result
                    elif name == 'mcx':
                        mcx_result = result
                except TimeoutError:
                    logger.error('%s section timed out', name)
                    run_times[name] = 300
                except Exception as exc:
                    logger.error('%s section failed: %s', name, exc)
                    run_times[name] = timer.time() - started
    else:
        if 'global' in sections_to_run:
            start = timer.time()
            global_result = run_global_markets()
            run_times['global'] = timer.time() - start
        if 'nifty' in sections_to_run:
            start = timer.time()
            nifty_result = run_nifty_analysis(mode='eod')
            run_times['nifty'] = timer.time() - start
        if 'fno' in sections_to_run:
            start = timer.time()
            fno_result = run_fno_scanner(mode='eod')
            run_times['fno'] = timer.time() - start
        if 'mcx' in sections_to_run:
            start = timer.time()
            mcx_result = run_mcx_scanner()
            run_times['mcx'] = timer.time() - start

    learning_summary = _build_learning_summary()
    full_report = consolidate_reports(
        started_at,
        global_result,
        nifty_result,
        fno_result,
        mcx_result,
        run_times,
        learning_summary,
        sections_to_run,
    )

    print(full_report)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    basename = f"morning_brief_{started_at.strftime('%Y%m%d_%H%M')}"
    text_path = output_dir / f'{basename}.txt'
    json_path = output_dir / f'{basename}.json'
    latest_text_path = output_dir / 'morning_brief_latest.txt'
    latest_json_path = output_dir / 'morning_brief_latest.json'

    text_path.write_text(full_report, encoding='utf-8')
    latest_text_path.write_text(full_report, encoding='utf-8')
    logger.info('Report saved to: %s', text_path)

    summary_lines = build_quick_summary_lines(global_result, nifty_result, fno_result, mcx_result)
    predictions = build_brief_predictions(global_result, nifty_result, fno_result, mcx_result)
    run_id = f"morning_brief_{started_at.strftime('%Y%m%d_%H%M%S')}"

    structured_payload = {
        'brief_run_id': run_id,
        'generated_at': started_at.isoformat(),
        'mode': args.mode,
        'market_phase': infer_market_phase(started_at),
        'report_path': str(text_path),
        'archive_db_path': str(DEFAULT_DB_PATH) if DEFAULT_DB_PATH else None,
        'quick_summary_lines': summary_lines,
        'learning_summary': learning_summary,
        'run_times': run_times,
        'sections': {
            'global': _serialize_section_result(global_result),
            'nifty': _serialize_section_result(nifty_result),
            'fno': _serialize_section_result(fno_result),
            'mcx': _serialize_section_result(mcx_result),
        },
        'predictions': [asdict(prediction) for prediction in predictions],
    }

    archive_enabled = PLATFORM_ARCHIVE_AVAILABLE and not args.no_archive
    if archive_enabled and archive_brief_run is not None and BriefRunRecord is not None:
        try:
            run_record = BriefRunRecord(
                brief_run_id=run_id,
                run_timestamp=started_at.isoformat(),
                run_date=started_at.date().isoformat(),
                session_label='morning_brief',
                mode=args.mode,
                market_phase=infer_market_phase(started_at),
                source_version='morning_brief.py',
                output_path=str(text_path),
                summary_text=' | '.join(summary_lines) if summary_lines else 'Morning brief completed',
                learning_summary_text=learning_summary,
                metadata={
                    'report_path': str(text_path),
                    'json_path': str(json_path),
                    'run_times': run_times,
                    'sections': structured_payload['sections'],
                    'prediction_count': len(predictions),
                },
            )
            archive_brief_run(run_record, predictions, DEFAULT_DB_PATH)
        except Exception as exc:
            logger.warning('Failed to archive brief run: %s', exc)
            structured_payload['archive_error'] = str(exc)

    json_path.write_text(json.dumps(structured_payload, indent=2), encoding='utf-8')
    latest_json_path.write_text(json.dumps(structured_payload, indent=2), encoding='utf-8')
    logger.info('Structured brief payload saved to: %s', json_path)

    success_count = sum([
        global_result.success,
        nifty_result.success,
        fno_result.success,
        mcx_result.success,
    ])
    total = len(sections_to_run)

    if args.notify:
        if success_count == total:
            send_notification('Morning Brief Ready', f'All {total} sections completed successfully', success=True)
        else:
            send_notification('Morning Brief Completed', f'{success_count}/{total} sections successful', success=success_count > 0)

    logger.info('=' * 60)
    logger.info('Morning Brief Completed: %s/%s sections successful', success_count, total)
    logger.info('Total time: %.1fs', sum(run_times.values()))
    logger.info('=' * 60)

    return 0 if success_count > 0 else 1


if __name__ == '__main__':
    sys.exit(main())
