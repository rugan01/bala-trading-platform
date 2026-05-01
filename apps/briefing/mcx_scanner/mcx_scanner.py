#!/usr/bin/env python3
"""
MCX Intraday Scanner
====================
Scans MCX commodities for intraday trading opportunities based on:
- Price position relative to day's range
- Volume analysis
- Risk-adjusted setups (max Rs.1000 risk per trade)

Watchlist (from mcx-commodities-intraday.md):
- SILVERMIC (Silver Micro - 1 kg)
- GOLDM (Gold Mini - 10 gm)
- ZINCMINI (Zinc Mini - 1 MT)
- CRUDEOILM (Crude Oil Mini - 10 bbl)
- NATGASMINI (Natural Gas Mini - 250 mmBtu)

Usage:
    python mcx_scanner.py [--risk 1000] [--output json|text]

Author: Bala's Product OS
Last Updated: April 2026
"""

import os
import sys
import json
import argparse
import requests
import gzip
import csv
import io
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_ENV_FILE = REPO_ROOT / '.env'

# =============================================================================
# CONFIGURATION
# =============================================================================

# MCX Watchlist with contract specifications
MCX_WATCHLIST = {
    'SILVERMIC': {
        'name': 'Silver Micro',
        'lot_size': 1,           # 1 kg
        'tick_size': 1.0,        # Rs.1 per kg (tick = Rs.100 in price terms)
        'price_quote': 'per kg',
        'margin_pct': 5,
    },
    'GOLDM': {
        'name': 'Gold Mini',
        'lot_size': 10,          # 10 grams (actually it should be 100g lot)
        'tick_size': 1.0,        # Rs.1 per 10g
        'price_quote': 'per 10g',
        'margin_pct': 4,
    },
    'ZINCMINI': {
        'name': 'Zinc Mini',
        'lot_size': 1000,        # 1 MT = 1000 kg
        'tick_size': 0.05,       # Rs.0.05 per kg
        'price_quote': 'per kg',
        'margin_pct': 6,
    },
    'CRUDEOILM': {
        'name': 'Crude Oil Mini',
        'lot_size': 10,          # 10 barrels
        'tick_size': 1.0,        # Rs.1 per barrel
        'price_quote': 'per bbl',
        'margin_pct': 5,
    },
    'NATGASMINI': {
        'name': 'Natural Gas Mini',
        'lot_size': 250,         # 250 mmBtu
        'tick_size': 0.10,       # Rs.0.10 per mmBtu
        'price_quote': 'per mmBtu',
        'margin_pct': 8,
    },
}

# Upstox API endpoints
UPSTOX_QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
UPSTOX_INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.csv.gz"


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class Instrument:
    """MCX instrument with contract details."""
    symbol: str
    trading_symbol: str
    instrument_key: str
    lot_size: int
    tick_size: float
    tick_value: float  # Rs per tick movement for 1 lot
    expiry: str
    name: str

@dataclass
class Quote:
    """Live market quote for an instrument."""
    symbol: str
    ltp: float
    open: float
    high: float
    low: float
    prev_close: float
    volume: int
    change_pct: float
    day_range: float
    range_ticks: int
    position_pct: float  # Position within day's range (0=low, 100=high)

@dataclass
class TradeSetup:
    """Trade recommendation with risk parameters."""
    symbol: str
    name: str
    direction: str  # LONG, SHORT, NEUTRAL
    reasoning: str
    ltp: float
    entry_low: float
    entry_high: float
    stop_loss: float
    target_1: float
    target_2: float
    risk_amount: float
    max_sl_ticks: int
    quantity: int
    lot_size: int
    expiry: str
    score: int
    volume: int


# =============================================================================
# API FUNCTIONS
# =============================================================================

def get_access_token() -> str:
    """Load Upstox access token from environment or .env file."""
    token = os.environ.get('UPSTOX_ACCESS_TOKEN')

    if not token:
        # Try loading from .env file
        env_paths = [
            DEFAULT_ENV_FILE,
            Path('.env'),
        ]
        for env_path in env_paths:
            if env_path.exists():
                with open(env_path) as f:
                    for line in f:
                        if line.startswith('UPSTOX_ACCESS_TOKEN'):
                            token = line.split('=', 1)[1].strip().strip("'\"")
                            break
                if token:
                    break

    if not token:
        raise ValueError("UPSTOX_ACCESS_TOKEN not found. Set it in environment or .env file.")

    return token


def download_instrument_master() -> List[Dict]:
    """Download and parse MCX instrument master file."""
    print("Downloading MCX instrument master...")
    resp = requests.get(UPSTOX_INSTRUMENTS_URL, timeout=30)
    resp.raise_for_status()

    content = gzip.decompress(resp.content).decode('utf-8')
    reader = csv.DictReader(io.StringIO(content))

    # Filter for commodity futures only
    futures = [row for row in reader if row.get('instrument_type') == 'FUTCOM']
    print(f"Found {len(futures)} MCX futures contracts")

    return futures


def find_watchlist_instruments(futures: List[Dict]) -> Dict[str, Instrument]:
    """Find nearest expiry contracts for watchlist symbols."""
    instruments = {}

    for symbol_prefix, config in MCX_WATCHLIST.items():
        # Find all contracts matching the symbol prefix
        matches = [
            f for f in futures
            if f.get('tradingsymbol', '').startswith(symbol_prefix)
        ]

        if not matches:
            print(f"Warning: No contracts found for {symbol_prefix}")
            continue

        # Sort by expiry to get nearest
        matches.sort(key=lambda x: x.get('expiry', ''))
        nearest = matches[0]

        # Get actual lot and tick from master data
        lot_size = int(float(nearest.get('lot_size', config['lot_size'])))
        tick_size = float(nearest.get('tick_size', config['tick_size']))

        # Tick value = lot_size * tick_size
        tick_value = lot_size * tick_size

        instruments[symbol_prefix] = Instrument(
            symbol=symbol_prefix,
            trading_symbol=nearest.get('tradingsymbol', ''),
            instrument_key=nearest.get('instrument_key', ''),
            lot_size=lot_size,
            tick_size=tick_size,
            tick_value=tick_value,
            expiry=nearest.get('expiry', ''),
            name=config['name'],
        )

        print(f"  {symbol_prefix}: {nearest.get('tradingsymbol')} | Lot: {lot_size} | Tick: {tick_size} | TickVal: Rs.{tick_value}")

    return instruments


def fetch_quote(instrument_key: str, token: str) -> Optional[Dict]:
    """Fetch live quote for a single instrument."""
    headers = {
        'Authorization': f'Bearer {token}',
        'Accept': 'application/json'
    }

    encoded_key = urllib.parse.quote(instrument_key)
    url = f"{UPSTOX_QUOTE_URL}?instrument_key={encoded_key}"

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        if data.get('status') == 'success' and data.get('data'):
            # Return first quote (should be only one)
            return list(data['data'].values())[0]
    except Exception as e:
        print(f"Error fetching quote for {instrument_key}: {e}")

    return None


def fetch_all_quotes(instruments: Dict[str, Instrument], token: str) -> Dict[str, Quote]:
    """Fetch quotes for all instruments."""
    quotes = {}

    print("\nFetching live quotes...")
    for symbol, inst in instruments.items():
        raw_quote = fetch_quote(inst.instrument_key, token)

        if not raw_quote:
            continue

        ltp = raw_quote.get('last_price', 0)
        ohlc = raw_quote.get('ohlc', {})
        open_p = ohlc.get('open', 0)
        high = ohlc.get('high', 0)
        low = ohlc.get('low', 0)
        prev_close = ohlc.get('close', 0)
        volume = raw_quote.get('volume', 0)

        if ltp == 0:
            continue

        day_range = high - low if high > 0 and low > 0 else 0
        change_pct = ((ltp - prev_close) / prev_close * 100) if prev_close else 0
        position_pct = ((ltp - low) / day_range * 100) if day_range > 0 else 50
        range_ticks = int(day_range / inst.tick_size) if inst.tick_size > 0 else 0

        quotes[symbol] = Quote(
            symbol=symbol,
            ltp=ltp,
            open=open_p,
            high=high,
            low=low,
            prev_close=prev_close,
            volume=volume,
            change_pct=change_pct,
            day_range=day_range,
            range_ticks=range_ticks,
            position_pct=position_pct,
        )

    return quotes


# =============================================================================
# ANALYSIS FUNCTIONS
# =============================================================================

def calculate_trade_setup(
    symbol: str,
    inst: Instrument,
    quote: Quote,
    max_risk: float = 1000
) -> TradeSetup:
    """Calculate trade setup with risk parameters."""

    # Max SL in ticks for given risk
    max_sl_ticks = int(max_risk / inst.tick_value) if inst.tick_value > 0 else 0
    sl_points = max_sl_ticks * inst.tick_size

    # Determine direction based on position in range
    if quote.position_pct <= 30:
        direction = "LONG"
        reasoning = "Near day low - bounce potential"
        entry_low = quote.low
        entry_high = quote.low + quote.day_range * 0.2
        stop_loss = quote.low - sl_points
        target_1 = quote.low + quote.day_range * 0.5
        target_2 = quote.high
    elif quote.position_pct >= 70:
        direction = "SHORT"
        reasoning = "Near day high - reversal potential"
        entry_low = quote.high - quote.day_range * 0.2
        entry_high = quote.high
        stop_loss = quote.high + sl_points
        target_1 = quote.high - quote.day_range * 0.5
        target_2 = quote.low
    else:
        direction = "NEUTRAL"
        reasoning = "Range middle - wait for breakout"
        entry_low = quote.high  # Breakout levels
        entry_high = quote.low
        stop_loss = sl_points  # As offset
        target_1 = quote.high + quote.day_range * 0.5
        target_2 = quote.low - quote.day_range * 0.5

    # Calculate score
    score = 0

    # Volume score
    if quote.volume > 10000: score += 4
    elif quote.volume > 5000: score += 3
    elif quote.volume > 1000: score += 2
    elif quote.volume > 100: score += 1

    # Position extremes score
    if quote.position_pct <= 15 or quote.position_pct >= 85: score += 4
    elif quote.position_pct <= 25 or quote.position_pct >= 75: score += 3
    elif quote.position_pct <= 35 or quote.position_pct >= 65: score += 2

    # R:R potential
    if max_sl_ticks > 0 and quote.range_ticks > 0:
        rr_potential = quote.range_ticks / max_sl_ticks
        if rr_potential >= 3: score += 3
        elif rr_potential >= 2: score += 2
        elif rr_potential >= 1: score += 1

    # Practical SL range
    if 5 <= max_sl_ticks <= 50: score += 2
    elif 3 <= max_sl_ticks <= 100: score += 1

    return TradeSetup(
        symbol=symbol,
        name=inst.name,
        direction=direction,
        reasoning=reasoning,
        ltp=quote.ltp,
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=stop_loss,
        target_1=target_1,
        target_2=target_2,
        risk_amount=min(max_risk, max_sl_ticks * inst.tick_value),
        max_sl_ticks=max_sl_ticks,
        quantity=1,
        lot_size=inst.lot_size,
        expiry=inst.expiry,
        score=score,
        volume=quote.volume,
    )


# =============================================================================
# OUTPUT FUNCTIONS
# =============================================================================

def print_text_report(
    instruments: Dict[str, Instrument],
    quotes: Dict[str, Quote],
    setups: List[TradeSetup],
    max_risk: float
):
    """Print formatted text report."""
    print("\n" + "=" * 100)
    print(f"MCX INTRADAY SCANNER - {datetime.now().strftime('%Y-%m-%d %H:%M IST')}")
    print(f"Risk Budget: Rs.{max_risk:.0f} per trade | Max Trades: 2")
    print("=" * 100)

    # Instrument specs
    print(f"\n{'Symbol':<15} {'Contract':<22} {'Lot':>8} {'Tick':>10} {'TickVal':>12} {'Expiry':<12}")
    print("-" * 85)
    for symbol, inst in instruments.items():
        print(f"{symbol:<15} {inst.trading_symbol:<22} {inst.lot_size:>8} {inst.tick_size:>10.2f} Rs.{inst.tick_value:>10.2f} {inst.expiry:<12}")

    # Live quotes
    print("\n" + "=" * 100)
    print("LIVE MARKET DATA")
    print("=" * 100)
    print(f"{'Symbol':<15} {'LTP':>12} {'Chg%':>8} {'Range':>12} {'Ticks':>8} {'Vol':>10} {'Pos%':>8}")
    print("-" * 85)

    for symbol, quote in sorted(quotes.items(), key=lambda x: x[1].volume, reverse=True):
        print(f"{symbol:<15} {quote.ltp:>12.2f} {quote.change_pct:>7.2f}% {quote.day_range:>12.2f} {quote.range_ticks:>8} {quote.volume:>10} {quote.position_pct:>7.1f}%")

    # Trade setups
    print("\n" + "=" * 100)
    print("TRADE RECOMMENDATIONS (Sorted by Score)")
    print("=" * 100)

    tradeable = [s for s in setups if s.volume >= 50 and s.direction != "NEUTRAL"]
    tradeable.sort(key=lambda x: x.score, reverse=True)

    for i, setup in enumerate(tradeable[:2], 1):
        print(f"\n{'=' * 70}")
        print(f"SETUP {i}: {setup.symbol} ({setup.name})")
        print(f"{'=' * 70}")
        print(f"Direction: {setup.direction}")
        print(f"Reasoning: {setup.reasoning}")
        print(f"LTP: Rs.{setup.ltp:.2f}")
        print(f"Entry Zone: Rs.{setup.entry_low:.2f} - Rs.{setup.entry_high:.2f}")
        print(f"Stop Loss: Rs.{setup.stop_loss:.2f} ({setup.max_sl_ticks} ticks)")
        print(f"Target 1: Rs.{setup.target_1:.2f}")
        print(f"Target 2: Rs.{setup.target_2:.2f}")
        print(f"Risk: Rs.{setup.risk_amount:.0f} | Qty: 1 lot ({setup.lot_size} units)")
        print(f"Volume: {setup.volume} | Score: {setup.score}/13")
        print(f"Expiry: {setup.expiry}")

    # Exit & Trailing Rules
    print("\n" + "=" * 100)
    print("EXIT & TRAILING RULES (Follow Religiously)")
    print("=" * 100)
    print("1. Split into 2 lots at entry")
    print("2. Book Lot 1 at 1:1 R:R (first target)")
    print("3. Trail Lot 2 with Supertrend 5,1.5 on 15-min chart")
    print("4. Exit Lot 2 when 15-min candle CLOSES below Supertrend")
    print("")
    print("KEY INSIGHT: In trending commodities, price NEVER closes below")
    print("both ST 5,1.5 and ST 5,3 on 15-min. Don't trail tighter than this!")
    print("")
    print("Session: 5 PM - 11:30 PM | Exit by 11 PM | No US data releases")


def output_json(
    instruments: Dict[str, Instrument],
    quotes: Dict[str, Quote],
    setups: List[TradeSetup],
    max_risk: float
) -> Dict:
    """Generate JSON output."""
    return {
        'timestamp': datetime.now().isoformat(),
        'max_risk': max_risk,
        'instruments': {k: asdict(v) for k, v in instruments.items()},
        'quotes': {k: asdict(v) for k, v in quotes.items()},
        'setups': [asdict(s) for s in sorted(setups, key=lambda x: x.score, reverse=True)],
    }


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='MCX Intraday Scanner')
    parser.add_argument('--risk', type=float, default=1000, help='Max risk per trade (Rs)')
    parser.add_argument('--output', choices=['text', 'json'], default='text', help='Output format')
    parser.add_argument('--save', type=str, help='Save output to file')
    args = parser.parse_args()

    try:
        # Get access token
        token = get_access_token()

        # Download and find instruments
        futures = download_instrument_master()
        instruments = find_watchlist_instruments(futures)

        if not instruments:
            print("No instruments found. Check watchlist configuration.")
            sys.exit(1)

        # Fetch quotes
        quotes = fetch_all_quotes(instruments, token)

        if not quotes:
            print("No quotes received. Market may be closed.")
            sys.exit(1)

        # Generate setups
        setups = []
        for symbol in quotes:
            if symbol in instruments:
                setup = calculate_trade_setup(
                    symbol, instruments[symbol], quotes[symbol], args.risk
                )
                setups.append(setup)

        # Output
        if args.output == 'json':
            result = output_json(instruments, quotes, setups, args.risk)
            output_str = json.dumps(result, indent=2, default=str)
            print(output_str)

            if args.save:
                with open(args.save, 'w') as f:
                    f.write(output_str)
                print(f"\nSaved to {args.save}")
        else:
            print_text_report(instruments, quotes, setups, args.risk)

    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()
