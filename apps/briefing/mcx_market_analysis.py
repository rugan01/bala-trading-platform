#!/usr/bin/env python3
"""
MCX Commodity Market Analysis Tool
==================================
Fetches live quotes and historical data for MCX commodities from Upstox API.

COMMON MISTAKES TO AVOID (documented for future reference):
===========================================================
1. INSTRUMENT KEY FORMAT:
   - WRONG: MCX_FO|GOLD25JUNFUT (guessed symbol-based format)
   - CORRECT: MCX_FO|487665 (numeric exchange token from instruments master)
   - Always download instruments master file to get correct keys

2. INSTRUMENTS MASTER URL:
   - URL: https://assets.upstox.com/market-quote/instruments/exchange/MCX.csv.gz
   - Must decompress gzip content
   - Headers: instrument_key, exchange_token, tradingsymbol, name, last_price,
              expiry, strike, tick_size, lot_size, instrument_type, option_type, exchange

3. FILTERING FUTURES vs OPTIONS:
   - Futures: instrument_type == 'FUTCOM'
   - Options: instrument_type == 'OPTFUT'
   - Must filter by expiry date >= today for active contracts

4. API ENDPOINTS:
   - Live Quotes: GET /v2/market-quote/quotes?instrument_key={comma_separated_keys}
   - LTP Only: GET /v2/market-quote/ltp?instrument_key={comma_separated_keys}
   - Historical: GET /v2/historical-candle/{instrument_key}/{interval}/{to_date}/{from_date}
   - Intervals: 1minute, 30minute, day, week, month

5. LIVE QUOTES RETURNING EMPTY:
   - This happens when market is closed or no recent trades
   - MCX trading hours: 9:00 AM - 11:30 PM IST (Mon-Fri)
   - Holidays: Check Upstox holiday calendar
   - Fallback: Use historical candles API for last available data

6. TOKEN FORMAT:
   - Token in .env has quotes: 'eyJ0...'
   - Must strip quotes when reading: token.strip("'\"")

Usage:
    python mcx_market_analysis.py                    # Full analysis with trade ideas
    python mcx_market_analysis.py --live-only       # Only live quotes
    python mcx_market_analysis.py --historical-only # Only historical analysis
    python mcx_market_analysis.py --comprehensive   # Full technical analysis with levels

REFERENCES:
    - Full API Documentation: see the local trading knowledge base / docs
    - Token Refresh: apps/journaling/upstox_token_refresh.py
"""

import os
import sys
import argparse
import requests
import gzip
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, NamedTuple
from dataclasses import dataclass
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / '.env'


# Constants
INSTRUMENTS_URL = 'https://assets.upstox.com/market-quote/instruments/exchange/MCX.csv.gz'
QUOTE_API = 'https://api.upstox.com/v2/market-quote/quotes'
LTP_API = 'https://api.upstox.com/v2/market-quote/ltp'
HISTORICAL_API = 'https://api.upstox.com/v2/historical-candle'

# Commodities to track with their trading symbol patterns
# IMPORTANT: Order matters - longer/more specific patterns must come first
# Uses regex-like matching with startswith + exclusion logic
COMMODITY_SYMBOL_PATTERNS = [
    # Gold variants - must exclude mini variants when matching main GOLD
    # Main GOLD: GOLD26..., GOLD27... but NOT GOLDM, GOLDPETAL, GOLDGUINEA, GOLDTEN
    ('GOLDGUINEA', 'GOLDGUINEA', None),
    ('GOLDPETAL', 'GOLDPETAL', None),
    ('GOLDTEN', 'GOLDTEN', None),
    ('GOLDM', 'GOLDM', None),
    ('GOLD', 'GOLD', ['GOLDM', 'GOLDPETAL', 'GOLDGUINEA', 'GOLDTEN']),  # Exclude mini variants
    # Silver variants
    ('SILVERMIC', 'SILVERMIC', None),
    ('SILVERM', 'SILVERM', None),
    ('SILVER', 'SILVER', ['SILVERM', 'SILVERMIC']),  # Exclude mini variants
    # Crude Oil
    ('CRUDEOILM', 'CRUDEOILM', None),  # Mini crude
    ('CRUDEOIL', 'CRUDEOIL', ['CRUDEOILM']),  # Exclude mini
    # Natural Gas
    ('NATGASMINI', 'NATGASMINI', None),
    ('NATURALGAS', 'NATURALGAS', ['NATGASMINI']),  # Exclude mini
    # Base metals
    ('COPPER', 'COPPER', None),
    ('ZINCMINI', 'ZINCMINI', None),
    ('ZINC', 'ZINC', ['ZINCMINI']),
    ('ALUMINIUM', 'ALUMINIUM', None),
    ('ALUMINI', 'ALUMINI', None),  # Mini aluminium (different name)
    ('LEADMINI', 'LEADMINI', None),
    ('LEAD', 'LEAD', ['LEADMINI']),
    ('NICKEL', 'NICKEL', None),
]

# Default commodities to display (main contracts)
DEFAULT_COMMODITIES = [
    'GOLD', 'GOLDM', 'SILVER', 'SILVERM',
    'CRUDEOIL', 'NATURALGAS',
    'COPPER', 'ZINC', 'ALUMINIUM', 'LEAD', 'NICKEL'
]


@dataclass
class Contract:
    """Represents an MCX futures contract."""
    commodity: str
    instrument_key: str
    trading_symbol: str
    expiry: datetime
    lot_size: int


@dataclass
class Quote:
    """Live quote data."""
    commodity: str
    ltp: float
    open: float
    high: float
    low: float
    close: float  # Previous close
    volume: int
    change: float
    pct_change: float


@dataclass
class Candle:
    """Historical candle data."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    oi: int


@dataclass
class TechnicalLevels:
    """Key technical levels for a commodity."""
    # Pivot Points
    pivot: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float
    # Swing levels
    swing_high: float
    swing_low: float
    # Recent range
    range_high: float
    range_low: float
    # ATR for volatility
    atr: float
    atr_pct: float


@dataclass
class MarketAnalysis:
    """Complete market analysis for a commodity."""
    commodity: str
    ltp: float
    prev_close: float
    change: float
    pct_change: float
    # Levels
    levels: TechnicalLevels
    # Trend analysis
    trend: str  # UPTREND, DOWNTREND, SIDEWAYS
    trend_strength: str  # STRONG, MODERATE, WEAK
    trend_probability: float  # 0-100%
    # Market structure
    structure: str  # TRENDING, RANGING, BREAKOUT, BREAKDOWN
    # Bias
    bias: str  # BULLISH, BEARISH, NEUTRAL
    bias_reason: str
    # Trade setup
    trade_type: str  # LONG, SHORT, NO_TRADE
    entry_zone: tuple  # (low, high)
    stop_loss: float
    target1: float
    target2: float
    risk_reward: float


class MCXAnalyzer:
    """MCX Market Analyzer using Upstox API."""

    def __init__(self, env_file: str = None):
        """Initialize with Upstox credentials from .env file."""
        env_file = env_file or str(DEFAULT_ENV_FILE)
        load_dotenv(env_file, override=True)

        # Get token - MUST strip quotes
        self.token = os.getenv('UPSTOX_BALA_ACCESS_TOKEN', '').strip("'\"")
        if not self.token:
            raise ValueError("UPSTOX_BALA_ACCESS_TOKEN not found in .env")

        self.headers = {
            'Authorization': f'Bearer {self.token}',
            'Content-Type': 'application/json',
            'Accept': 'application/json'
        }

        self.contracts: Dict[str, Contract] = {}

    def download_instruments_master(self) -> None:
        """
        Download MCX instruments master and extract active futures contracts.

        IMPORTANT: This is required to get correct instrument keys.
        Never guess instrument keys - always fetch from master file.
        """
        print("Downloading MCX instruments master...")

        response = requests.get(
            INSTRUMENTS_URL,
            headers={'User-Agent': 'Mozilla/5.0'},
            timeout=30
        )

        if response.status_code != 200:
            raise Exception(f"Failed to download instruments: {response.status_code}")

        csv_content = gzip.decompress(response.content).decode('utf-8')
        lines = csv_content.split('\n')

        today = datetime.now().date()
        futures_by_commodity: Dict[str, List[Contract]] = {}

        for line in lines[1:]:  # Skip header
            if not line.strip():
                continue

            parts = [p.strip('"') for p in line.split(',')]
            if len(parts) < 10:
                continue

            instrument_key = parts[0]
            trading_symbol = parts[2]
            name = parts[3]
            expiry_str = parts[5]
            lot_size = int(parts[8]) if parts[8].isdigit() else 1
            instrument_type = parts[9]

            # Only futures contracts (FUTCOM), not options (OPTFUT)
            if instrument_type != 'FUTCOM':
                continue

            # Match by trading symbol pattern with exclusions
            # e.g., GOLD26JUNFUT matches GOLD (main), not GOLDM/GOLDPETAL
            commodity = None
            for prefix, display_name, exclusions in COMMODITY_SYMBOL_PATTERNS:
                if trading_symbol.startswith(prefix):
                    # Check if any exclusion pattern matches
                    if exclusions:
                        excluded = any(trading_symbol.startswith(ex) for ex in exclusions)
                        if excluded:
                            continue  # Skip this pattern, try next
                    commodity = display_name
                    break

            if not commodity:
                continue

            # Only track commodities in our default list
            if commodity not in DEFAULT_COMMODITIES:
                continue

            # Parse expiry and check if active
            try:
                expiry = datetime.strptime(expiry_str, '%Y-%m-%d').date()
                if expiry < today:
                    continue  # Expired contract
            except ValueError:
                continue

            contract = Contract(
                commodity=commodity,
                instrument_key=instrument_key,
                trading_symbol=trading_symbol,
                expiry=expiry,
                lot_size=lot_size
            )

            if commodity not in futures_by_commodity:
                futures_by_commodity[commodity] = []
            futures_by_commodity[commodity].append(contract)

        # Select nearest expiry contract for each commodity
        for commodity in DEFAULT_COMMODITIES:
            if commodity in futures_by_commodity:
                contracts = sorted(futures_by_commodity[commodity], key=lambda c: c.expiry)
                self.contracts[commodity] = contracts[0]

        print(f"Found {len(self.contracts)} active commodity futures")

    def get_live_quotes(self) -> Dict[str, Quote]:
        """
        Fetch live quotes for all tracked commodities.

        Returns empty dict if market is closed or no data available.
        In that case, use get_historical_data() as fallback.

        NOTE: API request uses instrument_key format: MCX_FO|488290
              API response uses format: MCX_FO:CRUDEOIL26MAYFUT (exchange:tradingsymbol)
        """
        if not self.contracts:
            self.download_instruments_master()

        # Build comma-separated instrument keys
        keys = [c.instrument_key for c in self.contracts.values()]
        keys_param = ','.join(keys)

        url = f'{QUOTE_API}?instrument_key={keys_param}'
        response = requests.get(url, headers=self.headers)
        data = response.json()

        if data.get('status') != 'success':
            print(f"Quote API error: {data.get('errors', 'Unknown error')}")
            return {}

        quotes = {}
        quote_data = data.get('data', {})

        for commodity, contract in self.contracts.items():
            # Response key format is "MCX_FO:TRADINGSYMBOL" not "MCX_FO|token"
            # Build the expected response key from trading symbol
            response_key = f"MCX_FO:{contract.trading_symbol}"

            if response_key not in quote_data:
                # Try alternate format with instrument_key (| replaced with :)
                alt_key = contract.instrument_key.replace('|', ':')
                if alt_key in quote_data:
                    response_key = alt_key
                else:
                    continue

            q = quote_data[response_key]
            ltp = q.get('last_price', 0)
            if ltp == 0:
                continue  # No trade data

            ohlc = q.get('ohlc', {})
            # Use net_change from API (actual change from prev close)
            # ohlc.close is current day's close, not previous day's
            change = q.get('net_change', 0)
            prev_close = ltp - change if change else ohlc.get('close', ltp)
            pct_change = (change / prev_close * 100) if prev_close else 0

            quotes[commodity] = Quote(
                commodity=commodity,
                ltp=ltp,
                open=ohlc.get('open', 0),
                high=ohlc.get('high', 0),
                low=ohlc.get('low', 0),
                close=prev_close,
                volume=q.get('volume', 0),
                change=change,
                pct_change=pct_change
            )

        return quotes

    def get_historical_data(self, days: int = 30) -> Dict[str, List[Candle]]:
        """
        Fetch historical daily candles for all tracked commodities.

        This always works even when market is closed.
        """
        if not self.contracts:
            self.download_instruments_master()

        from_date = (datetime.now() - timedelta(days=days)).strftime('%Y-%m-%d')
        to_date = datetime.now().strftime('%Y-%m-%d')

        historical = {}

        for commodity, contract in self.contracts.items():
            url = f'{HISTORICAL_API}/{contract.instrument_key}/day/{to_date}/{from_date}'
            response = requests.get(url, headers=self.headers)
            data = response.json()

            if data.get('status') != 'success':
                continue

            candles_data = data.get('data', {}).get('candles', [])
            if not candles_data:
                continue

            candles = []
            for c in candles_data:
                # Format: [timestamp, open, high, low, close, volume, oi]
                candles.append(Candle(
                    timestamp=datetime.fromisoformat(c[0].replace('T', ' ').split('+')[0]),
                    open=c[1],
                    high=c[2],
                    low=c[3],
                    close=c[4],
                    volume=c[5],
                    oi=c[6] if len(c) > 6 else 0
                ))

            historical[commodity] = candles

        return historical

    def calculate_atr(self, candles: List[Candle], period: int = 14) -> float:
        """Calculate Average True Range."""
        if len(candles) < period + 1:
            return 0

        true_ranges = []
        # Candles are newest first
        for i in range(period):
            current = candles[i]
            prev = candles[i + 1]
            tr = max(
                current.high - current.low,
                abs(current.high - prev.close),
                abs(current.low - prev.close)
            )
            true_ranges.append(tr)

        return sum(true_ranges) / len(true_ranges)

    def calculate_pivot_points(self, candle: Candle) -> dict:
        """Calculate classic pivot points from a candle."""
        h, l, c = candle.high, candle.low, candle.close
        pivot = (h + l + c) / 3

        return {
            'pivot': pivot,
            'r1': 2 * pivot - l,
            'r2': pivot + (h - l),
            'r3': h + 2 * (pivot - l),
            's1': 2 * pivot - h,
            's2': pivot - (h - l),
            's3': l - 2 * (h - pivot),
        }

    def find_swing_levels(self, candles: List[Candle], lookback: int = 20) -> tuple:
        """Find swing high and swing low from recent candles."""
        if len(candles) < lookback:
            lookback = len(candles)

        recent = candles[:lookback]
        swing_high = max(c.high for c in recent)
        swing_low = min(c.low for c in recent)

        return swing_high, swing_low

    def calculate_trend_probability(self, candles: List[Candle], lookback: int = 20) -> tuple:
        """
        Calculate trend direction and probability using multiple factors:
        - Higher highs / higher lows pattern
        - Close position in range
        - Momentum (rate of change)
        - Directional movement

        Returns: (trend, strength, probability)
        """
        if len(candles) < lookback:
            return "SIDEWAYS", "WEAK", 50.0

        recent = candles[:lookback][::-1]  # Chronological order

        # Factor 1: Higher Highs / Higher Lows analysis
        highs = [c.high for c in recent]
        lows = [c.low for c in recent]
        closes = [c.close for c in recent]

        hh_count = sum(1 for i in range(1, len(highs)) if highs[i] > highs[i-1])
        hl_count = sum(1 for i in range(1, len(lows)) if lows[i] > lows[i-1])
        lh_count = sum(1 for i in range(1, len(highs)) if highs[i] < highs[i-1])
        ll_count = sum(1 for i in range(1, len(lows)) if lows[i] < lows[i-1])

        bullish_structure = (hh_count + hl_count) / (2 * (lookback - 1)) * 100
        bearish_structure = (lh_count + ll_count) / (2 * (lookback - 1)) * 100

        # Factor 2: Close position in recent range
        range_high = max(highs[-10:]) if len(highs) >= 10 else max(highs)
        range_low = min(lows[-10:]) if len(lows) >= 10 else min(lows)
        range_size = range_high - range_low

        if range_size > 0:
            close_position = (closes[-1] - range_low) / range_size * 100
        else:
            close_position = 50

        # Factor 3: Short-term momentum (5-day ROC)
        if len(closes) >= 5:
            momentum = (closes[-1] - closes[-5]) / closes[-5] * 100
        else:
            momentum = 0

        # Factor 4: Recent direction (last 5 candles)
        recent_5 = recent[-5:] if len(recent) >= 5 else recent
        up_days = sum(1 for c in recent_5 if c.close > c.open)
        down_days = len(recent_5) - up_days

        # Combine factors for trend determination
        bullish_score = 0
        bearish_score = 0

        # Structure weight: 40%
        bullish_score += bullish_structure * 0.4
        bearish_score += bearish_structure * 0.4

        # Close position weight: 25%
        bullish_score += close_position * 0.25
        bearish_score += (100 - close_position) * 0.25

        # Momentum weight: 20%
        if momentum > 0:
            bullish_score += min(momentum * 5, 20)  # Cap at 20
        else:
            bearish_score += min(abs(momentum) * 5, 20)

        # Recent direction weight: 15%
        bullish_score += (up_days / len(recent_5)) * 15
        bearish_score += (down_days / len(recent_5)) * 15

        # Determine trend
        diff = bullish_score - bearish_score

        if diff > 20:
            trend = "UPTREND"
            probability = min(50 + diff, 85)
            strength = "STRONG" if diff > 35 else "MODERATE"
        elif diff < -20:
            trend = "DOWNTREND"
            probability = min(50 + abs(diff), 85)
            strength = "STRONG" if diff < -35 else "MODERATE"
        else:
            trend = "SIDEWAYS"
            probability = 50 + abs(diff)
            strength = "WEAK"

        return trend, strength, round(probability, 1)

    def determine_market_structure(self, candles: List[Candle], atr: float) -> str:
        """Determine if market is trending, ranging, or at breakout/breakdown."""
        if len(candles) < 10:
            return "INSUFFICIENT DATA"

        recent = candles[:10]
        latest = recent[0]

        # Get 20-day range
        swing_high, swing_low = self.find_swing_levels(candles, 20)
        range_size = swing_high - swing_low

        # Check for breakout/breakdown
        if latest.close > swing_high - (atr * 0.5):
            return "BREAKOUT"
        elif latest.close < swing_low + (atr * 0.5):
            return "BREAKDOWN"

        # Check range compression (low volatility = ranging)
        recent_range = max(c.high for c in recent) - min(c.low for c in recent)
        if recent_range < range_size * 0.4:
            return "RANGING"

        return "TRENDING"

    def generate_comprehensive_analysis(self, commodity: str, candles: List[Candle],
                                         live_price: float = None) -> MarketAnalysis:
        """Generate complete technical analysis for a commodity."""
        if len(candles) < 2:
            return None

        latest = candles[0]
        prev = candles[1]

        # Use live price if available, else latest close
        ltp = live_price if live_price else latest.close
        prev_close = prev.close
        change = ltp - prev_close
        pct_change = (change / prev_close * 100) if prev_close else 0

        # Calculate technical levels
        atr = self.calculate_atr(candles)
        atr_pct = (atr / ltp * 100) if ltp else 0
        pivots = self.calculate_pivot_points(latest)
        swing_high, swing_low = self.find_swing_levels(candles, 20)

        levels = TechnicalLevels(
            pivot=round(pivots['pivot'], 2),
            r1=round(pivots['r1'], 2),
            r2=round(pivots['r2'], 2),
            r3=round(pivots['r3'], 2),
            s1=round(pivots['s1'], 2),
            s2=round(pivots['s2'], 2),
            s3=round(pivots['s3'], 2),
            swing_high=round(swing_high, 2),
            swing_low=round(swing_low, 2),
            range_high=round(latest.high, 2),
            range_low=round(latest.low, 2),
            atr=round(atr, 2),
            atr_pct=round(atr_pct, 2)
        )

        # Trend analysis with probability
        trend, strength, probability = self.calculate_trend_probability(candles)

        # Market structure
        structure = self.determine_market_structure(candles, atr)

        # Determine bias
        if trend == "UPTREND" and structure in ["TRENDING", "BREAKOUT"]:
            bias = "BULLISH"
            bias_reason = f"{strength} uptrend with {structure.lower()} structure"
        elif trend == "DOWNTREND" and structure in ["TRENDING", "BREAKDOWN"]:
            bias = "BEARISH"
            bias_reason = f"{strength} downtrend with {structure.lower()} structure"
        elif structure == "BREAKOUT":
            bias = "BULLISH"
            bias_reason = "Breakout above recent range"
        elif structure == "BREAKDOWN":
            bias = "BEARISH"
            bias_reason = "Breakdown below recent range"
        else:
            bias = "NEUTRAL"
            bias_reason = f"Sideways/ranging market, wait for direction"

        # Generate trade setup with proper R:R using ATR
        if bias == "BULLISH" and probability >= 60:
            trade_type = "LONG"
            # Entry zone: pullback entry near support or current price
            entry_low = max(ltp - atr * 0.5, levels.s1)
            entry_high = ltp
            # SL: Below day low with small buffer (tighter stop for better R:R)
            stop_loss = latest.low - atr * 0.2
            # Risk from mid-entry point
            mid_entry = (entry_low + entry_high) / 2
            risk = mid_entry - stop_loss
            # Targets: 1.5x and 2.5x risk
            target1 = mid_entry + risk * 1.5
            target2 = mid_entry + risk * 2.5
            risk_reward = 1.5  # Designed to be 1:1.5
        elif bias == "BEARISH" and probability >= 60:
            trade_type = "SHORT"
            # Entry zone: rally entry near resistance or current price
            entry_low = ltp
            entry_high = min(ltp + atr * 0.5, levels.r1)
            # SL: Above day high with small buffer
            stop_loss = latest.high + atr * 0.2
            # Risk from mid-entry point
            mid_entry = (entry_low + entry_high) / 2
            risk = stop_loss - mid_entry
            # Targets: 1.5x and 2.5x risk
            target1 = mid_entry - risk * 1.5
            target2 = mid_entry - risk * 2.5
            risk_reward = 1.5  # Designed to be 1:1.5
        else:
            trade_type = "NO_TRADE"
            entry_low = 0
            entry_high = 0
            stop_loss = 0
            target1 = 0
            target2 = 0
            risk_reward = 0

        return MarketAnalysis(
            commodity=commodity,
            ltp=round(ltp, 2),
            prev_close=round(prev_close, 2),
            change=round(change, 2),
            pct_change=round(pct_change, 2),
            levels=levels,
            trend=trend,
            trend_strength=strength,
            trend_probability=probability,
            structure=structure,
            bias=bias,
            bias_reason=bias_reason,
            trade_type=trade_type,
            entry_zone=(round(entry_low, 2), round(entry_high, 2)),
            stop_loss=round(stop_loss, 2),
            target1=round(target1, 2),
            target2=round(target2, 2),
            risk_reward=risk_reward
        )

    def analyze_trend(self, candles: List[Candle], lookback: int = 5) -> str:
        """Analyze trend based on recent candles (legacy method for backward compat)."""
        trend, strength, _ = self.calculate_trend_probability(candles, lookback)
        if strength == "STRONG":
            return f"STRONG {trend}"
        return trend

    def generate_trade_ideas(self, quotes: Dict[str, Quote], historical: Dict[str, List[Candle]]) -> List[str]:
        """Generate trade ideas based on price action (legacy method)."""
        ideas = []

        for commodity in DEFAULT_COMMODITIES:
            if commodity not in historical or not historical[commodity]:
                continue

            candles = historical[commodity]
            latest = candles[0]
            prev = candles[1] if len(candles) > 1 else latest

            change = latest.close - prev.close
            pct_change = (change / prev.close * 100) if prev.close else 0

            range_size = latest.high - latest.low
            if range_size <= 0:
                continue

            body_pos = (latest.close - latest.low) / range_size

            trend = self.analyze_trend(candles)

            # Long signals
            if pct_change > 0.5 and body_pos > 0.6 and 'UPTREND' in trend:
                sl = latest.low
                target = latest.close + (latest.close - sl)
                ideas.append(
                    f"LONG {commodity}: Entry ~{latest.close:.2f}, "
                    f"SL {sl:.2f}, Target {target:.2f} "
                    f"(Trend: {trend}, +{pct_change:.2f}%)"
                )

            # Short signals
            elif pct_change < -0.5 and body_pos < 0.4 and 'DOWNTREND' in trend:
                sl = latest.high
                target = latest.close - (sl - latest.close)
                ideas.append(
                    f"SHORT {commodity}: Entry ~{latest.close:.2f}, "
                    f"SL {sl:.2f}, Target {target:.2f} "
                    f"(Trend: {trend}, {pct_change:.2f}%)"
                )

        return ideas

    def print_comprehensive_analysis(self, analysis: MarketAnalysis):
        """Print detailed analysis for a single commodity."""
        a = analysis
        l = a.levels

        print(f"\n{'─'*80}")
        print(f"  {a.commodity}")
        print(f"{'─'*80}")

        # Price info
        chg_symbol = "+" if a.change >= 0 else ""
        print(f"  LTP: {a.ltp:.2f}  |  Change: {chg_symbol}{a.change:.2f} ({chg_symbol}{a.pct_change:.2f}%)")

        # Trend & Structure
        print(f"\n  TREND: {a.trend} ({a.trend_strength}) - {a.trend_probability}% probability")
        print(f"  STRUCTURE: {a.structure}")
        print(f"  BIAS: {a.bias} - {a.bias_reason}")

        # Key Levels
        print(f"\n  KEY LEVELS:")
        print(f"  {'─'*40}")
        print(f"  Swing High:  {l.swing_high:>12.2f}  (20-day)")
        print(f"  R2:          {l.r2:>12.2f}")
        print(f"  R1:          {l.r1:>12.2f}")
        print(f"  Pivot:       {l.pivot:>12.2f}")
        print(f"  S1:          {l.s1:>12.2f}")
        print(f"  S2:          {l.s2:>12.2f}")
        print(f"  Swing Low:   {l.swing_low:>12.2f}  (20-day)")
        print(f"  {'─'*40}")
        print(f"  ATR(14):     {l.atr:>12.2f}  ({l.atr_pct:.2f}% of price)")

        # Trade Setup
        print(f"\n  TRADE SETUP:")
        if a.trade_type == "NO_TRADE":
            print(f"  No clear setup - market is {a.structure.lower()}, wait for direction")
        else:
            print(f"  Direction:   {a.trade_type}")
            print(f"  Entry Zone:  {a.entry_zone[0]:.2f} - {a.entry_zone[1]:.2f}")
            print(f"  Stop Loss:   {a.stop_loss:.2f}")
            print(f"  Target 1:    {a.target1:.2f}")
            print(f"  Target 2:    {a.target2:.2f}")
            print(f"  Risk:Reward: 1:{a.risk_reward}")

    def print_analysis(self, include_live: bool = True, include_historical: bool = True,
                       comprehensive: bool = False):
        """Print full market analysis."""
        print(f"\n{'='*100}")
        print(f"MCX COMMODITY MARKET ANALYSIS - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'='*100}")

        # Download instruments master
        self.download_instruments_master()

        # Print active contracts
        print(f"\nActive Contracts:")
        print(f"{'-'*80}")
        print(f"{'Commodity':<12} {'Symbol':<25} {'Instrument Key':<20} {'Expiry':<12} {'Lot'}")
        print(f"{'-'*80}")
        for comm in DEFAULT_COMMODITIES:
            if comm in self.contracts:
                c = self.contracts[comm]
                print(f"{comm:<12} {c.trading_symbol:<25} {c.instrument_key:<20} {c.expiry} {c.lot_size}")

        quotes = {}
        historical = {}

        # Live quotes
        if include_live:
            print(f"\n{'='*100}")
            print("LIVE QUOTES")
            print(f"{'='*100}")

            quotes = self.get_live_quotes()

            if quotes:
                print(f"\n{'Commodity':<12} {'LTP':>12} {'Change':>12} {'%Chg':>8} {'Open':>12} {'High':>12} {'Low':>12} {'Volume':>12}")
                print(f"{'-'*100}")

                for comm in DEFAULT_COMMODITIES:
                    if comm in quotes:
                        q = quotes[comm]
                        print(f"{comm:<12} {q.ltp:>12.2f} {q.change:>+12.2f} {q.pct_change:>+7.2f}% {q.open:>12.2f} {q.high:>12.2f} {q.low:>12.2f} {q.volume:>12,}")
            else:
                print("\nNo live quotes available (market may be closed)")
                print("Falling back to historical data...")

        # Historical data
        if include_historical:
            print(f"\n{'='*100}")
            print("HISTORICAL DATA (Last Trading Day)")
            print(f"{'='*100}")

            historical = self.get_historical_data(days=30)

            if historical:
                print(f"\n{'Commodity':<12} {'Close':>12} {'Change':>12} {'%Chg':>8} {'Open':>12} {'High':>12} {'Low':>12} {'Volume':>12} {'Date'}")
                print(f"{'-'*110}")

                results = []
                for comm in DEFAULT_COMMODITIES:
                    if comm in historical and historical[comm]:
                        candles = historical[comm]
                        latest = candles[0]
                        prev = candles[1] if len(candles) > 1 else latest

                        change = latest.close - prev.close
                        pct_change = (change / prev.close * 100) if prev.close else 0

                        results.append({
                            'commodity': comm,
                            'close': latest.close,
                            'change': change,
                            'pct_change': pct_change,
                            'open': latest.open,
                            'high': latest.high,
                            'low': latest.low,
                            'volume': latest.volume,
                            'date': latest.timestamp.strftime('%Y-%m-%d')
                        })

                        print(f"{comm:<12} {latest.close:>12.2f} {change:>+12.2f} {pct_change:>+7.2f}% {latest.open:>12.2f} {latest.high:>12.2f} {latest.low:>12.2f} {latest.volume:>12,} {latest.timestamp.strftime('%Y-%m-%d')}")

                # Gainers and Losers
                if results:
                    gainers = sorted([r for r in results if r['pct_change'] > 0], key=lambda x: x['pct_change'], reverse=True)
                    losers = sorted([r for r in results if r['pct_change'] < 0], key=lambda x: x['pct_change'])

                    print(f"\n{'='*100}")
                    print("MARKET SUMMARY")
                    print(f"{'='*100}")

                    print("\nTOP GAINERS:")
                    for g in gainers[:5]:
                        print(f"   {g['commodity']}: +{g['pct_change']:.2f}%")

                    if losers:
                        print("\nTOP LOSERS:")
                        for l in losers[:5]:
                            print(f"   {l['commodity']}: {l['pct_change']:.2f}%")

                    # Trend Analysis
                    print(f"\n{'='*100}")
                    print("TREND ANALYSIS (5-day)")
                    print(f"{'='*100}")

                    for comm in DEFAULT_COMMODITIES:
                        if comm in historical:
                            trend = self.analyze_trend(historical[comm])
                            print(f"   {comm:<12}: {trend}")

                    # Comprehensive analysis or simple trade ideas
                    if comprehensive:
                        print(f"\n{'='*100}")
                        print("COMPREHENSIVE TECHNICAL ANALYSIS")
                        print(f"{'='*100}")

                        analyses = []
                        for comm in DEFAULT_COMMODITIES:
                            if comm in historical and historical[comm]:
                                live_price = quotes[comm].ltp if comm in quotes else None
                                analysis = self.generate_comprehensive_analysis(
                                    comm, historical[comm], live_price
                                )
                                if analysis:
                                    analyses.append(analysis)
                                    self.print_comprehensive_analysis(analysis)

                        # Summary table
                        if analyses:
                            print(f"\n{'='*100}")
                            print("SUMMARY - ACTIONABLE TRADES")
                            print(f"{'='*100}")
                            print(f"\n{'Commodity':<12} {'Bias':<10} {'Trend':<12} {'Prob':>6} {'Structure':<12} {'Trade':<8} {'Entry Zone':<20} {'SL':>10} {'Target':>10} {'R:R':>6}")
                            print(f"{'-'*120}")

                            for a in analyses:
                                entry_str = f"{a.entry_zone[0]:.0f}-{a.entry_zone[1]:.0f}" if a.trade_type != "NO_TRADE" else "-"
                                sl_str = f"{a.stop_loss:.0f}" if a.trade_type != "NO_TRADE" else "-"
                                tgt_str = f"{a.target1:.0f}" if a.trade_type != "NO_TRADE" else "-"
                                rr_str = f"1:{a.risk_reward}" if a.trade_type != "NO_TRADE" else "-"

                                print(f"{a.commodity:<12} {a.bias:<10} {a.trend:<12} {a.trend_probability:>5.0f}% {a.structure:<12} {a.trade_type:<8} {entry_str:<20} {sl_str:>10} {tgt_str:>10} {rr_str:>6}")

                            # High probability trades
                            high_prob = [a for a in analyses if a.trade_type != "NO_TRADE" and a.trend_probability >= 65]
                            if high_prob:
                                print(f"\n{'='*100}")
                                print("HIGH PROBABILITY SETUPS (>= 65%)")
                                print(f"{'='*100}")
                                for a in sorted(high_prob, key=lambda x: x.trend_probability, reverse=True):
                                    print(f"\n  {a.commodity}: {a.trade_type} @ {a.entry_zone[0]:.2f}-{a.entry_zone[1]:.2f}")
                                    print(f"  Reason: {a.bias_reason}")
                                    print(f"  Probability: {a.trend_probability}% | R:R = 1:{a.risk_reward}")
                                    print(f"  SL: {a.stop_loss:.2f} | T1: {a.target1:.2f} | T2: {a.target2:.2f}")
                    else:
                        # Simple trade ideas (legacy)
                        print(f"\n{'='*100}")
                        print("TRADE IDEAS")
                        print(f"{'='*100}")

                        ideas = self.generate_trade_ideas(quotes, historical)
                        if ideas:
                            for idea in ideas:
                                print(f"   {idea}")
                        else:
                            print("   No clear trade setups at the moment")
            else:
                print("\nNo historical data available")

        print(f"\n{'='*100}")
        print("Analysis complete")
        print(f"{'='*100}\n")


def main():
    parser = argparse.ArgumentParser(description='MCX Commodity Market Analysis')
    parser.add_argument('--live-only', action='store_true', help='Only show live quotes')
    parser.add_argument('--historical-only', action='store_true', help='Only show historical data')
    parser.add_argument('--comprehensive', '-c', action='store_true',
                        help='Full technical analysis with levels, trend probability, and trade setups')
    parser.add_argument('--env-file', default=None, help='Path to .env file')
    args = parser.parse_args()

    try:
        analyzer = MCXAnalyzer(env_file=args.env_file)

        include_live = not args.historical_only
        include_historical = not args.live_only

        analyzer.print_analysis(
            include_live=include_live,
            include_historical=include_historical,
            comprehensive=args.comprehensive
        )

    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == '__main__':
    main()
