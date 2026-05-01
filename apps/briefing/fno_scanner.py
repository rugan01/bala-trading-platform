#!/usr/bin/env python3
"""
F&O Stock Scanner
=================
Scans NSE F&O universe for bullish and bearish stocks based on:
- Sector relative strength vs Nifty 50
- 52-week high/low proximity
- Moving average alignment (20, 50, 200 EMA)
- Momentum (RSI)
- Volume confirmation

Default data source is Upstox. Yahoo is kept only as an explicit fallback path.

Usage:
    python fno_scanner.py                             # Upstox EOD scan
    python fno_scanner.py --mode live --json         # Upstox live scan
    python fno_scanner.py --source yahoo --top 10    # Fallback scan
"""

import os
import sys
import io
import json
import gzip
import argparse
import logging
from datetime import date, datetime, timedelta
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import quote

import requests
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / '.env'


# =============================================================================
# CONFIGURATION
# =============================================================================

SECTOR_INDICES = {
    'NIFTY 50': {
        'yahoo_symbol': '^NSEI',
        'upstox_aliases': ['Nifty 50', 'NIFTY 50'],
    },
    'NIFTY BANK': {
        'yahoo_symbol': '^NSEBANK',
        'upstox_aliases': ['Nifty Bank', 'NIFTY BANK'],
    },
    'NIFTY IT': {
        'yahoo_symbol': '^CNXIT',
        'upstox_aliases': ['Nifty IT', 'NIFTY IT'],
    },
    'NIFTY AUTO': {
        'yahoo_symbol': 'NIFTY_AUTO.NS',
        'upstox_aliases': ['Nifty Auto', 'NIFTY AUTO'],
    },
    'NIFTY PHARMA': {
        'yahoo_symbol': '^CNXPHARMA',
        'upstox_aliases': ['Nifty Pharma', 'NIFTY PHARMA'],
    },
    'NIFTY METAL': {
        'yahoo_symbol': '^CNXMETAL',
        'upstox_aliases': ['Nifty Metal', 'NIFTY METAL'],
    },
    'NIFTY REALTY': {
        'yahoo_symbol': '^CNXREALTY',
        'upstox_aliases': ['Nifty Realty', 'NIFTY REALTY'],
    },
    'NIFTY ENERGY': {
        'yahoo_symbol': '^CNXENERGY',
        'upstox_aliases': ['Nifty Energy', 'NIFTY ENERGY'],
    },
    'NIFTY FMCG': {
        'yahoo_symbol': '^CNXFMCG',
        'upstox_aliases': ['Nifty FMCG', 'NIFTY FMCG'],
    },
    'NIFTY INFRA': {
        'yahoo_symbol': '^CNXINFRA',
        'upstox_aliases': ['Nifty Infrastructure', 'NIFTY INFRA', 'Nifty Infra'],
    },
    'NIFTY PSE': {
        'yahoo_symbol': '^CNXPSE',
        'upstox_aliases': ['Nifty PSE', 'NIFTY PSE'],
    },
    'NIFTY MEDIA': {
        'yahoo_symbol': '^CNXMEDIA',
        'upstox_aliases': ['Nifty Media', 'NIFTY MEDIA'],
    },
    'NIFTY FIN SERVICE': {
        'yahoo_symbol': 'NIFTY_FIN_SERVICE.NS',
        'upstox_aliases': ['Nifty Financial Services', 'NIFTY FIN SERVICE', 'NIFTY FINANCIAL SERVICES'],
    },
    'NIFTY CONSUMPTION': {
        'yahoo_symbol': 'NIFTY_CONSR_DURBL.NS',
        'upstox_aliases': ['Nifty India Consumption', 'NIFTY CONSUMPTION'],
    },
}

# F&O Universe - Major stocks mapped to sectors
FNO_UNIVERSE = {
    'BANKING': [
        'HDFCBANK', 'ICICIBANK', 'SBIN', 'KOTAKBANK', 'AXISBANK',
        'INDUSINDBK', 'BANKBARODA', 'PNB', 'FEDERALBNK', 'IDFCFIRSTB',
        'BANDHANBNK', 'AUBANK', 'CANBK', 'IDBI'
    ],
    'NBFC': [
        'BAJFINANCE', 'BAJAJFINSV', 'CHOLAFIN', 'SHRIRAMFIN', 'MUTHOOTFIN',
        'M&MFIN', 'LICHSGFIN', 'PFC', 'RECLTD', 'SBICARD', 'MANAPPURAM'
    ],
    'IT': [
        'TCS', 'INFY', 'HCLTECH', 'WIPRO', 'TECHM', 'LTIM', 'COFORGE',
        'MPHASIS', 'PERSISTENT', 'LTTS'
    ],
    'AUTO': [
        'TATAMOTORS', 'M&M', 'MARUTI', 'BAJAJ-AUTO', 'HEROMOTOCO',
        'EICHERMOT', 'ASHOKLEY', 'TVSMOTOR', 'MOTHERSON', 'BOSCHLTD',
        'MRF', 'APOLLOTYRE', 'BALKRISIND', 'BHARATFORG', 'EXIDEIND'
    ],
    'PHARMA': [
        'SUNPHARMA', 'DRREDDY', 'CIPLA', 'DIVISLAB', 'APOLLOHOSP',
        'BIOCON', 'LUPIN', 'AUROPHARMA', 'ZYDUSLIFE', 'TORNTPHARM',
        'ALKEM', 'IPCALAB', 'LAURUSLABS', 'GLENMARK', 'MAXHEALTH'
    ],
    'METAL': [
        'TATASTEEL', 'JSWSTEEL', 'HINDALCO', 'VEDL', 'COALINDIA',
        'JINDALSTEL', 'SAIL', 'NMDC', 'NATIONALUM', 'APLAPOLLO'
    ],
    'ENERGY': [
        'RELIANCE', 'ONGC', 'IOC', 'BPCL', 'GAIL', 'NTPC', 'POWERGRID',
        'ADANIGREEN', 'ADANIPOWER', 'TATAPOWER', 'ADANIENT', 'PETRONET',
        'HINDPETRO', 'ATGL'
    ],
    'FMCG': [
        'HINDUNILVR', 'ITC', 'NESTLEIND', 'BRITANNIA', 'TATACONSUM',
        'DABUR', 'MARICO', 'GODREJCP', 'COLPAL', 'EMAMILTD',
        'PIDILITIND', 'BERGEPAINT', 'ASIANPAINT', 'UBL', 'VBL'
    ],
    'REALTY': [
        'DLF', 'GODREJPROP', 'OBEROIRLTY', 'PHOENIXLTD', 'PRESTIGE',
        'BRIGADE', 'LODHA', 'SOBHA'
    ],
    'INFRA': [
        'LT', 'ADANIPORTS', 'GMRINFRA', 'IRB', 'IRCTC', 'CONCOR'
    ],
    'TELECOM': [
        'BHARTIARTL', 'IDEA', 'TTML'
    ],
    'CEMENT': [
        'ULTRACEMCO', 'SHREECEM', 'AMBUJACEM', 'ACC', 'DALBHARAT',
        'RAMCOCEM', 'JKCEMENT', 'INDIACEM'
    ],
    'CAPGOODS': [
        'SIEMENS', 'ABB', 'HAVELLS', 'VOLTAS', 'CUMMINSIND',
        'BEL', 'BHEL', 'HAL', 'FACT', 'POLYCAB', 'CGPOWER',
        'THERMAX', 'GRINDWELL', 'KAJARIACER'
    ],
    'DIVERSIFIED': [
        'TITAN', 'TRENT', 'ZOMATO', 'PAYTM', 'NYKAA', 'DMART',
        'PAGEIND', 'ABFRL', 'MANYAVAR', 'DEVYANI', 'JUBLFOOD',
        'INDHOTEL', 'LEMON TREE', 'VIP'
    ],
    'INSURANCE': [
        'SBILIFE', 'HDFCLIFE', 'ICICIPRULI', 'ICICIGI', 'NIACL',
        'GICRE', 'STARHEALTH'
    ],
}

ALL_FNO_STOCKS = {}
for sector, stocks in FNO_UNIVERSE.items():
    for stock in stocks:
        ALL_FNO_STOCKS[stock] = sector

PERIOD_TO_CALENDAR_DAYS = {
    '1y': 370,
    '6mo': 190,
    '3mo': 95,
    '1mo': 45,
}


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


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class SectorData:
    name: str
    symbol: str
    price: float
    change_1d: float
    change_5d: float
    change_20d: float
    rs_vs_nifty: float
    status: str


@dataclass
class StockData:
    symbol: str
    name: str
    sector: str
    price: float
    change_1d: float
    change_5d: float
    change_20d: float
    high_52w: float
    low_52w: float
    pct_from_52w_high: float
    pct_from_52w_low: float
    ema_20: float
    ema_50: float
    ema_200: float
    above_ema_20: bool
    above_ema_50: bool
    above_ema_200: bool
    rsi_14: float
    avg_volume: int
    current_volume: int
    volume_ratio: float
    rs_vs_nifty: float
    score: int
    signal: str
    comparison_ready: bool = True
    special_status: str = 'normal'
    notes: str = ''


@dataclass
class ScannerReport:
    timestamp: str
    nifty_price: float
    nifty_change: float
    sectors: List[SectorData]
    bullish_stocks: List[StockData]
    bearish_stocks: List[StockData]
    skipped_stocks: List[StockData]
    strong_sectors: List[str]
    weak_sectors: List[str]
    data_source: str = 'upstox'
    data_mode: str = 'eod'


# =============================================================================
# YAHOO FALLBACK
# =============================================================================

def fetch_yahoo_data(symbol: str, period: str = '1y') -> Optional[Dict]:
    try:
        yahoo_symbol = symbol
        if not symbol.startswith('^') and not symbol.endswith('.NS'):
            yahoo_symbol = f"{symbol}.NS"

        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}"
        period_map = {
            '1y': ('1d', '1y'),
            '6mo': ('1d', '6mo'),
            '3mo': ('1d', '3mo'),
            '1mo': ('1d', '1mo'),
        }
        interval, range_val = period_map.get(period, ('1d', '1y'))
        params = {'interval': interval, 'range': range_val}
        headers = {'User-Agent': 'Mozilla/5.0'}

        response = requests.get(url, params=params, headers=headers, timeout=15)
        response.raise_for_status()

        data = response.json()
        result = data.get('chart', {}).get('result', [{}])[0]
        if not result:
            return None

        meta = result.get('meta', {})
        timestamps = result.get('timestamp', [])
        quote = result.get('indicators', {}).get('quote', [{}])[0]
        if not timestamps or not quote:
            return None

        closes = [c for c in quote.get('close', []) if c is not None]
        highs = [h for h in quote.get('high', []) if h is not None]
        lows = [l for l in quote.get('low', []) if l is not None]
        volumes = [v for v in quote.get('volume', []) if v is not None]
        if not closes:
            return None

        return {
            'symbol': symbol,
            'current_price': meta.get('regularMarketPrice', closes[-1]),
            'prev_close': meta.get('previousClose', closes[-2] if len(closes) > 1 else closes[-1]),
            'closes': closes,
            'highs': highs,
            'lows': lows,
            'volumes': volumes,
            'high_52w': max(highs) if highs else 0,
            'low_52w': min(lows) if lows else 0,
            'data_source': 'yahoo',
            'data_mode': 'fallback',
        }
    except Exception as exc:
        logger.debug('Failed to fetch %s from Yahoo: %s', symbol, exc)
        return None


# =============================================================================
# UPSTOX PROVIDER
# =============================================================================

class UpstoxDataProvider:
    BASE_URL = 'https://api.upstox.com/v2'
    INSTRUMENTS_URL = 'https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz'

    def __init__(self, access_token: str):
        self.access_token = access_token.strip().strip("'\"")
        self.headers = {
            'Accept': 'application/json',
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {self.access_token}',
        }
        self._instruments: Optional[List[Dict]] = None
        self._equity_cache: Dict[str, Dict] = {}
        self._index_cache: Dict[str, Dict] = {}
        self._history_cache: Dict[Tuple[str, str], List[Dict]] = {}
        self._quote_cache: Dict[str, Dict] = {}

    @staticmethod
    def _normalize(value: str) -> str:
        return ''.join(ch for ch in str(value).upper() if ch.isalnum())

    def _load_instruments(self) -> List[Dict]:
        if self._instruments is not None:
            return self._instruments

        response = requests.get(self.INSTRUMENTS_URL, timeout=30)
        response.raise_for_status()
        with gzip.GzipFile(fileobj=io.BytesIO(response.content)) as zipped:
            self._instruments = json.loads(zipped.read().decode('utf-8'))
        return self._instruments

    def _build_instrument(self, inst: Dict, display_symbol: str) -> Dict:
        return {
            'symbol': display_symbol,
            'instrument_key': inst.get('instrument_key'),
            'trading_symbol': inst.get('trading_symbol') or display_symbol,
            'segment': inst.get('segment'),
            'instrument_type': inst.get('instrument_type'),
            'lot_size': int(inst.get('lot_size') or 1),
        }

    def resolve_equity_symbol(self, symbol: str) -> Dict:
        key = self._normalize(symbol)
        if key in self._equity_cache:
            return self._equity_cache[key]

        for inst in self._load_instruments():
            if inst.get('segment') != 'NSE_EQ':
                continue
            trading_symbol = inst.get('trading_symbol') or ''
            if self._normalize(trading_symbol) == key:
                result = self._build_instrument(inst, symbol)
                self._equity_cache[key] = result
                return result

        raise KeyError(f'Could not resolve NSE equity instrument for {symbol}')

    def resolve_index_symbol(self, display_name: str, aliases: Optional[List[str]] = None) -> Dict:
        search_terms = {self._normalize(display_name)}
        for alias in aliases or []:
            search_terms.add(self._normalize(alias))

        cache_key = '|'.join(sorted(search_terms))
        if cache_key in self._index_cache:
            return self._index_cache[cache_key]

        for inst in self._load_instruments():
            if inst.get('segment') != 'NSE_INDEX':
                continue
            candidates = {
                self._normalize(inst.get('trading_symbol', '')),
                self._normalize(inst.get('short_name', '')),
                self._normalize(inst.get('name', '')),
            }
            if search_terms & candidates:
                result = self._build_instrument(inst, display_name)
                self._index_cache[cache_key] = result
                return result

        raise KeyError(f'Could not resolve NSE index instrument for {display_name}')

    def resolve_symbol(self, symbol: str, kind: str = 'equity', aliases: Optional[List[str]] = None) -> Dict:
        if kind == 'index':
            return self.resolve_index_symbol(symbol, aliases=aliases)
        return self.resolve_equity_symbol(symbol)

    def preload_quotes(self, instrument_keys: List[str]) -> None:
        missing = [key for key in sorted(set(instrument_keys)) if key and key not in self._quote_cache]
        if not missing:
            return

        chunk_size = 400
        for start in range(0, len(missing), chunk_size):
            chunk = missing[start:start + chunk_size]
            response = requests.get(
                f'{self.BASE_URL}/market-quote/quotes',
                headers=self.headers,
                params={'instrument_key': ','.join(chunk)},
                timeout=20,
            )
            response.raise_for_status()
            payload = response.json().get('data', {})
            for quote_payload in payload.values():
                instrument_key = quote_payload.get('instrument_token') or quote_payload.get('instrument_key')
                if instrument_key:
                    self._quote_cache[instrument_key] = quote_payload

    def get_quote(self, instrument_key: str) -> Dict:
        if instrument_key not in self._quote_cache:
            self.preload_quotes([instrument_key])
        return self._quote_cache.get(instrument_key, {})

    def get_daily_history(self, instrument_key: str, period: str = '1y') -> List[Dict]:
        cache_key = (instrument_key, period)
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]

        to_date = date.today()
        from_date = to_date - timedelta(days=PERIOD_TO_CALENDAR_DAYS.get(period, 370))
        encoded_key = quote(instrument_key, safe='')
        url = f'{self.BASE_URL}/historical-candle/{encoded_key}/day/{to_date.isoformat()}/{from_date.isoformat()}'
        response = requests.get(url, headers=self.headers, timeout=20)
        response.raise_for_status()
        candles = response.json().get('data', {}).get('candles', [])

        parsed = []
        for candle in reversed(candles):
            parsed.append({
                'timestamp': candle[0],
                'open': float(candle[1]),
                'high': float(candle[2]),
                'low': float(candle[3]),
                'close': float(candle[4]),
                'volume': int(candle[5] or 0),
            })

        self._history_cache[cache_key] = parsed
        return parsed

    def get_market_data(
        self,
        symbol: str,
        period: str = '1y',
        mode: str = 'eod',
        kind: str = 'equity',
        aliases: Optional[List[str]] = None,
    ) -> Optional[Dict]:
        instrument = self.resolve_symbol(symbol, kind=kind, aliases=aliases)
        history = self.get_daily_history(instrument['instrument_key'], period)
        if len(history) < 25:
            return None

        closed_history = list(history)
        if closed_history:
            latest_date = _extract_candle_date(closed_history[-1].get('timestamp'))
            if latest_date == date.today() and len(closed_history) > 25:
                closed_history = closed_history[:-1]

        if len(closed_history) < 25:
            return None

        base_closes = [candle['close'] for candle in closed_history]
        base_highs = [candle['high'] for candle in closed_history]
        base_lows = [candle['low'] for candle in closed_history]
        base_volumes = [candle['volume'] for candle in closed_history]

        quote_payload = None
        suspected_corporate_action = False
        suspicion_reason = ''
        historical_last_close = base_closes[-1]
        if mode == 'live':
            quote_payload = self.get_quote(instrument['instrument_key'])
            current_price = float(quote_payload.get('last_price') or base_closes[-1])
            net_change = quote_payload.get('net_change')
            if net_change is not None:
                prev_close = current_price - float(net_change)
            else:
                prev_close = base_closes[-1]
            ohlc = quote_payload.get('ohlc', {})
            highs = base_highs + [float(ohlc.get('high') or current_price)]
            lows = base_lows + [float(ohlc.get('low') or current_price)]
            closes = base_closes + [current_price]
            volumes = base_volumes + [int(quote_payload.get('volume') or 0)]
            if historical_last_close:
                quote_alignment_gap = abs(prev_close - historical_last_close) / historical_last_close if prev_close else None
                current_gap = abs(current_price - historical_last_close) / historical_last_close
                if (
                    quote_alignment_gap is not None
                    and quote_alignment_gap <= 0.03
                    and current_gap >= 0.35
                ):
                    suspected_corporate_action = True
                    suspicion_reason = (
                        'Live quote diverges sharply from the previous daily close while the quote-implied '
                        'previous close still matches the historical series. This usually means a split, bonus, '
                        'demerger, or another corporate-action discontinuity.'
                    )
        else:
            latest_candle = closed_history[-1]
            previous_candle = closed_history[-2] if len(closed_history) > 1 else latest_candle
            current_price = float(latest_candle['close'])
            prev_close = float(previous_candle['close'])
            highs = base_highs
            lows = base_lows
            closes = base_closes
            volumes = base_volumes

        recent_highs = highs[-252:] if len(highs) >= 252 else highs
        recent_lows = lows[-252:] if len(lows) >= 252 else lows

        return {
            'symbol': symbol,
            'instrument_key': instrument['instrument_key'],
            'trading_symbol': instrument['trading_symbol'],
            'current_price': current_price,
            'prev_close': prev_close,
            'closes': closes,
            'highs': highs,
            'lows': lows,
            'volumes': volumes,
            'high_52w': max(recent_highs) if recent_highs else current_price,
            'low_52w': min(recent_lows) if recent_lows else current_price,
            'quote': quote_payload,
            'data_source': 'upstox',
            'data_mode': mode,
            'historical_last_close': historical_last_close,
            'suspected_corporate_action': suspected_corporate_action,
            'suspicion_reason': suspicion_reason,
        }


def get_upstox_provider(env_file: Optional[str] = None) -> UpstoxDataProvider:
    env_path = env_file or str(DEFAULT_ENV_FILE)
    load_dotenv(env_path)
    token = os.getenv('UPSTOX_ACCESS_TOKEN', '').strip().strip("'\"")
    if not token:
        raise ValueError('UPSTOX_ACCESS_TOKEN not found')
    return UpstoxDataProvider(token)


# =============================================================================
# CALCULATION HELPERS
# =============================================================================

def calculate_ema(prices: List[float], period: int) -> float:
    if len(prices) < period:
        return prices[-1] if prices else 0

    multiplier = 2 / (period + 1)
    ema = sum(prices[:period]) / period
    for price in prices[period:]:
        ema = (price * multiplier) + (ema * (1 - multiplier))
    return ema


def calculate_rsi(prices: List[float], period: int = 14) -> float:
    if len(prices) < period + 1:
        return 50

    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    gains = [change if change > 0 else 0 for change in changes[-period:]]
    losses = [-change if change < 0 else 0 for change in changes[-period:]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100

    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi, 2)


def calculate_relative_strength(stock_returns: float, benchmark_returns: float) -> float:
    if benchmark_returns == 0:
        return 0
    return round(stock_returns - benchmark_returns, 2)


def calculate_period_return(closes: List[float], days: int) -> float:
    if len(closes) < days + 1:
        return 0
    start_price = closes[-(days + 1)]
    end_price = closes[-1]
    if start_price == 0:
        return 0
    return round(((end_price - start_price) / start_price) * 100, 2)


def fetch_market_data(
    symbol: str,
    period: str = '1y',
    *,
    source: str = 'upstox',
    mode: str = 'eod',
    provider: Optional[UpstoxDataProvider] = None,
    kind: str = 'equity',
    aliases: Optional[List[str]] = None,
    yahoo_symbol: Optional[str] = None,
) -> Optional[Dict]:
    if source == 'yahoo':
        return fetch_yahoo_data(yahoo_symbol or symbol, period)

    if provider is None:
        provider = get_upstox_provider()
    return provider.get_market_data(symbol, period=period, mode=mode, kind=kind, aliases=aliases)


# =============================================================================
# SECTOR ANALYSIS
# =============================================================================

def analyze_sectors(
    nifty_data: Dict,
    *,
    source: str = 'upstox',
    mode: str = 'eod',
    provider: Optional[UpstoxDataProvider] = None,
) -> List[SectorData]:
    sectors: List[SectorData] = []
    nifty_returns_5d = calculate_period_return(nifty_data['closes'], 5)
    nifty_returns_20d = calculate_period_return(nifty_data['closes'], 20)

    logger.info('Analyzing sectors using %s (%s mode)...', source, mode)

    if source == 'upstox' and provider is not None and mode == 'live':
        keys = []
        for sector_name, config in SECTOR_INDICES.items():
            if sector_name == 'NIFTY 50':
                continue
            try:
                inst = provider.resolve_symbol(sector_name, kind='index', aliases=config.get('upstox_aliases'))
                keys.append(inst['instrument_key'])
            except Exception as exc:
                logger.debug('Could not preload sector quote for %s: %s', sector_name, exc)
        provider.preload_quotes(keys)

    for sector_name, config in SECTOR_INDICES.items():
        if sector_name == 'NIFTY 50':
            continue

        data = fetch_market_data(
            sector_name,
            '3mo',
            source=source,
            mode=mode,
            provider=provider,
            kind='index',
            aliases=config.get('upstox_aliases'),
            yahoo_symbol=config.get('yahoo_symbol'),
        )
        if not data:
            continue

        change_1d = round(((data['current_price'] - data['prev_close']) / data['prev_close']) * 100, 2) if data['prev_close'] else 0
        change_5d = calculate_period_return(data['closes'], 5)
        change_20d = calculate_period_return(data['closes'], 20)
        rs = calculate_relative_strength(change_20d, nifty_returns_20d)

        if rs > 3:
            status = 'OUTPERFORMING'
        elif rs < -3:
            status = 'UNDERPERFORMING'
        else:
            status = 'NEUTRAL'

        sectors.append(SectorData(
            name=sector_name,
            symbol=sector_name,
            price=round(data['current_price'], 2),
            change_1d=change_1d,
            change_5d=change_5d,
            change_20d=change_20d,
            rs_vs_nifty=rs,
            status=status,
        ))

    sectors.sort(key=lambda item: item.rs_vs_nifty, reverse=True)
    return sectors


# =============================================================================
# STOCK ANALYSIS
# =============================================================================

def analyze_stock(
    symbol: str,
    sector: str,
    nifty_returns_20d: float,
    *,
    source: str = 'upstox',
    mode: str = 'eod',
    provider: Optional[UpstoxDataProvider] = None,
) -> Optional[StockData]:
    data = fetch_market_data(symbol, '1y', source=source, mode=mode, provider=provider, kind='equity')
    if not data or len(data['closes']) < 200:
        return None

    closes = data['closes']
    current_price = data['current_price']
    ema_20 = calculate_ema(closes, 20)
    ema_50 = calculate_ema(closes, 50)
    ema_200 = calculate_ema(closes, 200)
    rsi = calculate_rsi(closes, 14)

    change_1d = round(((current_price - data['prev_close']) / data['prev_close']) * 100, 2) if data['prev_close'] else 0
    change_5d = calculate_period_return(closes, 5)
    change_20d = calculate_period_return(closes, 20)

    high_52w = data['high_52w']
    low_52w = data['low_52w']
    pct_from_high = round(((current_price - high_52w) / high_52w) * 100, 2) if high_52w > 0 else 0
    pct_from_low = round(((current_price - low_52w) / low_52w) * 100, 2) if low_52w > 0 else 0

    volumes = data['volumes']
    avg_volume = int(sum(volumes[-20:]) / 20) if len(volumes) >= 20 else int(sum(volumes) / len(volumes))
    current_volume = int(volumes[-1]) if volumes else 0
    volume_ratio = round(current_volume / avg_volume, 2) if avg_volume > 0 else 1

    rs = calculate_relative_strength(change_20d, nifty_returns_20d)

    score = 0
    above_20 = current_price > ema_20
    above_50 = current_price > ema_50
    above_200 = current_price > ema_200

    if above_20:
        score += 10
    if above_50:
        score += 10
    if above_200:
        score += 10

    if ema_20 > ema_50 > ema_200:
        score += 15
    elif ema_20 < ema_50 < ema_200:
        score -= 15

    if pct_from_high > -5:
        score += 20
    elif pct_from_high > -10:
        score += 15
    elif pct_from_high > -20:
        score += 10
    elif pct_from_high < -40:
        score -= 15

    if rsi > 60:
        score += 15
    elif rsi > 50:
        score += 10
    elif rsi < 30:
        score -= 15
    elif rsi < 40:
        score -= 10

    if rs > 5:
        score += 15
    elif rs > 2:
        score += 10
    elif rs < -5:
        score -= 15
    elif rs < -2:
        score -= 10

    if volume_ratio > 1.5 and change_1d > 0:
        score += 10
    elif volume_ratio > 1.2 and change_1d > 0:
        score += 5
    elif volume_ratio > 1.5 and change_1d < 0:
        score -= 10

    if change_5d > 3:
        score += 10
    elif change_5d > 1:
        score += 5
    elif change_5d < -3:
        score -= 10
    elif change_5d < -1:
        score -= 5

    if score >= 60:
        signal = 'STRONG_BUY'
    elif score >= 40:
        signal = 'BUY'
    elif score <= -60:
        signal = 'STRONG_SELL'
    elif score <= -40:
        signal = 'SELL'
    else:
        signal = 'NEUTRAL'

    comparison_ready = not bool(data.get('suspected_corporate_action'))
    special_status = 'suspected_corporate_action' if not comparison_ready else 'normal'
    notes = str(data.get('suspicion_reason') or '')
    if mode == 'live' and not comparison_ready:
        signal = 'SKIP'
        score = 0

    return StockData(
        symbol=symbol,
        name=symbol,
        sector=sector,
        price=round(current_price, 2),
        change_1d=change_1d,
        change_5d=change_5d,
        change_20d=change_20d,
        high_52w=round(high_52w, 2),
        low_52w=round(low_52w, 2),
        pct_from_52w_high=pct_from_high,
        pct_from_52w_low=pct_from_low,
        ema_20=round(ema_20, 2),
        ema_50=round(ema_50, 2),
        ema_200=round(ema_200, 2),
        above_ema_20=above_20,
        above_ema_50=above_50,
        above_ema_200=above_200,
        rsi_14=rsi,
        avg_volume=avg_volume,
        current_volume=current_volume,
        volume_ratio=volume_ratio,
        rs_vs_nifty=rs,
        score=score,
        signal=signal,
        comparison_ready=comparison_ready,
        special_status=special_status,
        notes=notes,
    )


def scan_stocks(
    nifty_data: Dict,
    sectors: List[SectorData],
    max_workers: int = 10,
    *,
    source: str = 'upstox',
    mode: str = 'eod',
    provider: Optional[UpstoxDataProvider] = None,
) -> Tuple[List[StockData], List[StockData], List[StockData]]:
    nifty_returns_20d = calculate_period_return(nifty_data['closes'], 20)
    strong_sectors = [sector.name for sector in sectors if sector.status == 'OUTPERFORMING']
    weak_sectors = [sector.name for sector in sectors if sector.status == 'UNDERPERFORMING']

    logger.info('Strong sectors: %s', strong_sectors)
    logger.info('Weak sectors: %s', weak_sectors)

    all_stocks: List[StockData] = []
    sector_mapping = {
        'NIFTY BANK': 'BANKING',
        'NIFTY IT': 'IT',
        'NIFTY AUTO': 'AUTO',
        'NIFTY PHARMA': 'PHARMA',
        'NIFTY METAL': 'METAL',
        'NIFTY ENERGY': 'ENERGY',
        'NIFTY FMCG': 'FMCG',
        'NIFTY REALTY': 'REALTY',
        'NIFTY INFRA': 'INFRA',
        'NIFTY FIN SERVICE': 'NBFC',
    }

    priority_stocks = []
    for sector_name in strong_sectors + weak_sectors:
        fno_key = sector_mapping.get(sector_name)
        if fno_key and fno_key in FNO_UNIVERSE:
            priority_stocks.extend((stock, fno_key) for stock in FNO_UNIVERSE[fno_key])

    remaining_stocks = []
    for stock, sector in ALL_FNO_STOCKS.items():
        if not any(item[0] == stock for item in priority_stocks):
            remaining_stocks.append((stock, sector))

    stocks_to_scan = priority_stocks + remaining_stocks[:50]
    logger.info('Scanning %s stocks using %s (%s mode)...', len(stocks_to_scan), source, mode)

    if source == 'upstox' and provider is not None and mode == 'live':
        keys = []
        for stock, _sector in stocks_to_scan:
            try:
                inst = provider.resolve_symbol(stock, kind='equity')
                keys.append(inst['instrument_key'])
            except Exception as exc:
                logger.debug('Could not preload quote for %s: %s', stock, exc)
        provider.preload_quotes(keys)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_stock = {
            executor.submit(
                analyze_stock,
                stock,
                sector,
                nifty_returns_20d,
                source=source,
                mode=mode,
                provider=provider,
            ): (stock, sector)
            for stock, sector in stocks_to_scan
        }

        for index, future in enumerate(as_completed(future_to_stock), 1):
            stock, _sector = future_to_stock[future]
            try:
                result = future.result()
                if result:
                    all_stocks.append(result)
                    if index % 20 == 0:
                        logger.info('Processed %s/%s stocks...', index, len(stocks_to_scan))
            except Exception as exc:
                logger.debug('Error analyzing %s: %s', stock, exc)

    skipped = [stock for stock in all_stocks if stock.signal == 'SKIP']
    bullish = [stock for stock in all_stocks if stock.signal in ('STRONG_BUY', 'BUY')]
    bearish = [stock for stock in all_stocks if stock.signal in ('STRONG_SELL', 'SELL')]
    bullish.sort(key=lambda item: item.score, reverse=True)
    bearish.sort(key=lambda item: item.score)
    return bullish, bearish, skipped


# =============================================================================
# REPORT GENERATION
# =============================================================================

def generate_report(
    nifty_data: Dict,
    sectors: List[SectorData],
    bullish: List[StockData],
    bearish: List[StockData],
    skipped: List[StockData],
    top_n: int = 10,
    *,
    data_source: str = 'upstox',
    data_mode: str = 'eod',
) -> ScannerReport:
    strong_sectors = [sector.name for sector in sectors if sector.status == 'OUTPERFORMING']
    weak_sectors = [sector.name for sector in sectors if sector.status == 'UNDERPERFORMING']
    nifty_change = round(((nifty_data['current_price'] - nifty_data['prev_close']) / nifty_data['prev_close']) * 100, 2) if nifty_data['prev_close'] else 0

    return ScannerReport(
        timestamp=datetime.now().isoformat(),
        nifty_price=round(nifty_data['current_price'], 2),
        nifty_change=nifty_change,
        sectors=sectors,
        bullish_stocks=bullish[:top_n],
        bearish_stocks=bearish[:top_n],
        skipped_stocks=skipped,
        strong_sectors=strong_sectors,
        weak_sectors=weak_sectors,
        data_source=data_source,
        data_mode=data_mode,
    )


def format_text_report(report: ScannerReport) -> str:
    lines = []
    lines.append('=' * 90)
    lines.append(f"F&O STOCK SCANNER - {datetime.now().strftime('%B %d, %Y %H:%M IST')}")
    lines.append('=' * 90)
    lines.append(f"Data Source: {report.data_source.upper()} | Mode: {report.data_mode.upper()}")
    lines.append('')
    lines.append(f"NIFTY 50: {report.nifty_price:,.2f} ({report.nifty_change:+.2f}%)")
    lines.append('')
    lines.append('SECTOR RELATIVE STRENGTH vs NIFTY')
    lines.append('-' * 70)
    lines.append(f"{'Sector':<25} {'Price':>10} {'1D':>8} {'5D':>8} {'20D':>8} {'RS':>8} {'Status':<15}")
    lines.append('-' * 70)

    for sector in report.sectors:
        lines.append(
            f"{sector.name:<25} {sector.price:>10,.0f} {sector.change_1d:>+7.2f}% {sector.change_5d:>+7.2f}% "
            f"{sector.change_20d:>+7.2f}% {sector.rs_vs_nifty:>+7.2f} [{sector.status}]"
        )
    lines.append('')
    lines.append(f"STRONG SECTORS: {', '.join(report.strong_sectors) if report.strong_sectors else 'None'}")
    lines.append(f"WEAK SECTORS: {', '.join(report.weak_sectors) if report.weak_sectors else 'None'}")
    lines.append('')

    lines.append('=' * 90)
    lines.append('BULLISH STOCKS (Strong Trend, Near 52W High, Above MAs, Good Momentum)')
    lines.append('=' * 90)
    lines.append(f"{'Rank':<5} {'Symbol':<12} {'Sector':<10} {'Price':>10} {'%52WH':>8} {'RSI':>6} {'RS':>7} {'Score':>6} {'Signal':<12}")
    lines.append('-' * 90)

    for index, stock in enumerate(report.bullish_stocks, 1):
        lines.append(
            f"{index:<5} {stock.symbol:<12} {stock.sector:<10} {stock.price:>10,.2f} "
            f"{stock.pct_from_52w_high:>+7.1f}% {stock.rsi_14:>5.1f} {stock.rs_vs_nifty:>+6.1f} "
            f"{stock.score:>6} [{stock.signal}]"
        )

    if report.bullish_stocks:
        lines.append('')
        lines.append('TOP 3 BULLISH DETAILED:')
        lines.append('-' * 70)
        for stock in report.bullish_stocks[:3]:
            lines.append(f"  {stock.symbol} ({stock.sector})")
            lines.append(f"    Price: Rs.{stock.price:,.2f} | 52W High: Rs.{stock.high_52w:,.2f} ({stock.pct_from_52w_high:+.1f}%)")
            lines.append(f"    EMAs: 20={stock.ema_20:,.0f} | 50={stock.ema_50:,.0f} | 200={stock.ema_200:,.0f}")
            ma_aligned = 'YES' if stock.above_ema_20 and stock.above_ema_50 and stock.above_ema_200 else 'NO'
            lines.append(f"    Above All MAs: {ma_aligned} | RSI: {stock.rsi_14:.1f} | RS vs Nifty: {stock.rs_vs_nifty:+.1f}")
            lines.append(f"    Returns: 1D={stock.change_1d:+.2f}% | 5D={stock.change_5d:+.2f}% | 20D={stock.change_20d:+.2f}%")
            lines.append(f"    Volume: {stock.current_volume:,} ({stock.volume_ratio:.1f}x avg)")
            lines.append('')

    lines.append('=' * 90)
    lines.append('BEARISH STOCKS (Weak Trend, Near 52W Low, Below MAs, Poor Momentum)')
    lines.append('=' * 90)
    lines.append(f"{'Rank':<5} {'Symbol':<12} {'Sector':<10} {'Price':>10} {'%52WL':>8} {'RSI':>6} {'RS':>7} {'Score':>6} {'Signal':<12}")
    lines.append('-' * 90)

    for index, stock in enumerate(report.bearish_stocks, 1):
        lines.append(
            f"{index:<5} {stock.symbol:<12} {stock.sector:<10} {stock.price:>10,.2f} "
            f"{stock.pct_from_52w_low:>+7.1f}% {stock.rsi_14:>5.1f} {stock.rs_vs_nifty:>+6.1f} "
            f"{stock.score:>6} [{stock.signal}]"
        )

    if report.bearish_stocks:
        lines.append('')
        lines.append('TOP 3 BEARISH DETAILED:')
        lines.append('-' * 70)
        for stock in report.bearish_stocks[:3]:
            lines.append(f"  {stock.symbol} ({stock.sector})")
            lines.append(f"    Price: Rs.{stock.price:,.2f} | 52W Low: Rs.{stock.low_52w:,.2f} ({stock.pct_from_52w_low:+.1f}% from low)")
            lines.append(f"    EMAs: 20={stock.ema_20:,.0f} | 50={stock.ema_50:,.0f} | 200={stock.ema_200:,.0f}")
            below_all = 'YES' if not stock.above_ema_20 and not stock.above_ema_50 and not stock.above_ema_200 else 'NO'
            lines.append(f"    Below All MAs: {below_all} | RSI: {stock.rsi_14:.1f} | RS vs Nifty: {stock.rs_vs_nifty:+.1f}")
            lines.append(f"    Returns: 1D={stock.change_1d:+.2f}% | 5D={stock.change_5d:+.2f}% | 20D={stock.change_20d:+.2f}%")
            lines.append('')

    lines.append('=' * 90)
    lines.append('TRADING NOTES')
    lines.append('=' * 90)
    if report.data_mode == 'live':
        lines.append('- This is a live Upstox scan using LTP, current session volume, and daily trend context.')
        lines.append('- Use it as an intraday structural check against the morning brief, not as a blind entry signal.')
        if report.skipped_stocks:
            skipped_symbols = ', '.join(stock.symbol for stock in report.skipped_stocks[:8])
            lines.append(f'- Skipped live comparison for: {skipped_symbols} due to suspected corporate-action/series discontinuity.')
    else:
        lines.append('- This is an EOD Upstox scan using only closed-day data for regime selection.')
        lines.append('- Use it to decide which stocks deserve attention before market open.')
    lines.append('- Focus on STRONG_BUY for longs, STRONG_SELL for shorts')
    lines.append('- Confirm with price action before entry')
    lines.append('- Best entries on pullbacks to 20 EMA in direction of trend')
    lines.append('')
    lines.append('=' * 90)
    return '\n'.join(lines)


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='F&O Stock Scanner')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--top', type=int, default=10, help='Number of top stocks to show')
    parser.add_argument('--save', type=str, help='Save output to file')
    parser.add_argument('--source', choices=['upstox', 'yahoo'], default='upstox', help='Market data source')
    parser.add_argument('--mode', choices=['eod', 'live'], default='eod', help='Whether to use closed-day or live intraday prices')
    parser.add_argument('--env-file', type=str, default=str(DEFAULT_ENV_FILE), help='Path to .env file')
    parser.add_argument('--max-workers', type=int, default=5, help='Maximum parallel workers for stock analysis')
    args = parser.parse_args()

    try:
        provider = get_upstox_provider(args.env_file) if args.source == 'upstox' else None

        logger.info('Fetching Nifty 50 data from %s (%s mode)...', args.source, args.mode)
        nifty_config = SECTOR_INDICES['NIFTY 50']
        nifty_data = fetch_market_data(
            'NIFTY 50',
            '1y',
            source=args.source,
            mode=args.mode,
            provider=provider,
            kind='index',
            aliases=nifty_config.get('upstox_aliases'),
            yahoo_symbol=nifty_config.get('yahoo_symbol'),
        )
        if not nifty_data:
            logger.error('Failed to fetch Nifty data')
            return 1

        logger.info('Nifty 50: %s', f"{nifty_data['current_price']:,.2f}")
        sectors = analyze_sectors(nifty_data, source=args.source, mode=args.mode, provider=provider)
        bullish, bearish, skipped = scan_stocks(
            nifty_data,
            sectors,
            max_workers=args.max_workers,
            source=args.source,
            mode=args.mode,
            provider=provider,
        )
        logger.info('Found %s bullish and %s bearish stocks', len(bullish), len(bearish))

        report = generate_report(
            nifty_data,
            sectors,
            bullish,
            bearish,
            skipped,
            args.top,
            data_source=args.source,
            data_mode=args.mode,
        )

        if args.json:
            output = {
                'timestamp': report.timestamp,
                'nifty_price': report.nifty_price,
                'nifty_change': report.nifty_change,
                'strong_sectors': report.strong_sectors,
                'weak_sectors': report.weak_sectors,
                'data_source': report.data_source,
                'data_mode': report.data_mode,
                'sectors': [asdict(sector) for sector in report.sectors],
                'bullish_stocks': [asdict(stock) for stock in report.bullish_stocks],
                'bearish_stocks': [asdict(stock) for stock in report.bearish_stocks],
                'skipped_stocks': [asdict(stock) for stock in report.skipped_stocks],
            }
            output_str = json.dumps(output, indent=2)
            print(output_str)
            if args.save:
                Path(args.save).write_text(output_str, encoding='utf-8')
        else:
            text_report = format_text_report(report)
            print(text_report)
            if args.save:
                Path(args.save).write_text(text_report, encoding='utf-8')

        return 0
    except Exception as exc:
        logger.error('Scanner failed: %s', exc)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
