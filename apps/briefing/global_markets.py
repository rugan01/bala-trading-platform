#!/usr/bin/env python3
"""
Global Markets Analysis Module
==============================
Fetches and analyzes overnight global market movements:

- US Futures (24hr): S&P, Nasdaq, Dow futures - current vs yesterday close
- Asian Markets: Gap at open + intraday move + total change
- Commodities (24hr): Crude, Gold, Silver, Natural Gas - overnight move
- Currencies (24hr): DXY, USD/INR, major pairs - overnight move

Usage:
    python global_markets.py              # Print global market summary
    python global_markets.py --json       # Output as JSON

This module is designed to be imported by morning_brief.py
"""

import os
import sys
import json
import argparse
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, asdict, field
from typing import Optional, Dict, List

import requests
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format='%(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class MarketData:
    """Single market/instrument data with open/close breakdown."""
    symbol: str
    name: str
    price: float
    prev_close: float
    open: float
    change: float           # Total change from prev close
    change_pct: float       # Total % change from prev close
    gap: float              # Gap at open (open - prev_close)
    gap_pct: float          # Gap % at open
    intraday: float         # Intraday move (current - open)
    intraday_pct: float     # Intraday % move
    status: str             # BULLISH, BEARISH, NEUTRAL
    market_status: str      # OPEN, CLOSED, PRE-MARKET
    timestamp: Optional[str] = None


@dataclass
class GlobalMarketsReport:
    """Complete global markets report."""
    timestamp: str
    us_futures: Dict
    asian_markets: Dict
    commodities: Dict
    currencies: Dict
    gift_nifty: Optional[Dict]
    risk_sentiment: str
    dxy_impact: str
    bullish_signals: List[str] = field(default_factory=list)
    bearish_signals: List[str] = field(default_factory=list)
    overall_bias: str = "NEUTRAL"


# =============================================================================
# API FUNCTIONS
# =============================================================================

def fetch_yahoo_quote_detailed(symbol: str, *, quiet: bool = False) -> Optional[dict]:
    """
    Fetch detailed quote from Yahoo Finance API with OHLC data.
    Returns current, open, prev_close for proper gap/intraday calculation.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
        # Get 5 days of data to ensure we have prev close even after weekends
        params = {"interval": "1d", "range": "5d"}
        headers = {"User-Agent": "Mozilla/5.0"}

        response = requests.get(url, params=params, headers=headers, timeout=10)
        response.raise_for_status()

        data = response.json()
        result = data.get('chart', {}).get('result', [{}])[0]

        if not result:
            return None

        meta = result.get('meta', {})
        timestamps = result.get('timestamp', [])
        quote = result.get('indicators', {}).get('quote', [{}])[0]

        opens = quote.get('open', [])
        highs = quote.get('high', [])
        lows = quote.get('low', [])
        closes = quote.get('close', [])

        # Get current/live price
        current = meta.get('regularMarketPrice', 0)

        # Get today's open (last element in opens array)
        today_open = opens[-1] if opens and opens[-1] else current

        # Get previous close (second to last close, or from meta)
        prev_close = meta.get('previousClose', 0)
        if not prev_close and len(closes) >= 2:
            prev_close = closes[-2] if closes[-2] else current

        # Market state
        market_state = meta.get('marketState', 'CLOSED')

        # Calculate changes
        if prev_close and prev_close > 0:
            change = current - prev_close
            change_pct = (change / prev_close) * 100
        else:
            change = 0
            change_pct = 0

        # Gap (open vs prev close)
        if prev_close and prev_close > 0 and today_open:
            gap = today_open - prev_close
            gap_pct = (gap / prev_close) * 100
        else:
            gap = 0
            gap_pct = 0

        # Intraday (current vs open)
        if today_open and today_open > 0:
            intraday = current - today_open
            intraday_pct = (intraday / today_open) * 100
        else:
            intraday = 0
            intraday_pct = 0

        return {
            'price': current,
            'open': today_open,
            'prev_close': prev_close,
            'change': change,
            'change_pct': change_pct,
            'gap': gap,
            'gap_pct': gap_pct,
            'intraday': intraday,
            'intraday_pct': intraday_pct,
            'market_state': market_state,
            'high': highs[-1] if highs and highs[-1] else current,
            'low': lows[-1] if lows and lows[-1] else current,
        }

    except Exception as e:
        if not quiet:
            logger.warning(f"Failed to fetch {symbol}: {e}")
        return None


def determine_status(change_pct: float, thresholds: tuple = (-0.3, 0.3)) -> str:
    """Determine bullish/bearish/neutral status."""
    if change_pct > thresholds[1]:
        return "BULLISH"
    elif change_pct < thresholds[0]:
        return "BEARISH"
    return "NEUTRAL"


def create_market_data(key: str, symbol: str, name: str, data: dict,
                       custom_status_fn=None) -> MarketData:
    """Create MarketData object from raw data."""

    if custom_status_fn:
        status = custom_status_fn(data['change_pct'])
    else:
        status = determine_status(data['change_pct'])

    # Map Yahoo market state to our format
    market_state = data.get('market_state', 'CLOSED')
    if market_state in ('REGULAR', 'OPEN'):
        market_status = 'OPEN'
    elif market_state in ('PRE', 'PREPRE'):
        market_status = 'PRE-MARKET'
    elif market_state == 'POST':
        market_status = 'AFTER-HOURS'
    else:
        market_status = 'CLOSED'

    return MarketData(
        symbol=symbol,
        name=name,
        price=round(data['price'], 2),
        prev_close=round(data['prev_close'], 2),
        open=round(data['open'], 2),
        change=round(data['change'], 2),
        change_pct=round(data['change_pct'], 2),
        gap=round(data['gap'], 2),
        gap_pct=round(data['gap_pct'], 2),
        intraday=round(data['intraday'], 2),
        intraday_pct=round(data['intraday_pct'], 2),
        status=status,
        market_status=market_status
    )


# =============================================================================
# MARKET DATA FETCHERS
# =============================================================================

def fetch_us_futures() -> dict:
    """
    Fetch US index futures (24-hour markets).
    These trade almost 24/5, so we compare current price vs yesterday's close.
    """
    symbols = {
        'sp500': ('ES=F', 'S&P 500 Futures'),
        'nasdaq': ('NQ=F', 'Nasdaq Futures'),
        'dow': ('YM=F', 'Dow Futures'),
        'vix': ('^VIX', 'CBOE VIX'),
        'russell': ('RTY=F', 'Russell 2000 Futures'),
    }

    results = {}
    for key, (symbol, name) in symbols.items():
        data = fetch_yahoo_quote_detailed(symbol)
        if data:
            # VIX interpretation is inverted
            if key == 'vix':
                def vix_status(pct):
                    if pct > 10: return "FEAR"
                    if pct > 5: return "CAUTIOUS"
                    if pct < -5: return "COMPLACENT"
                    return "NEUTRAL"
                results[key] = create_market_data(key, symbol, name, data, vix_status)
            else:
                results[key] = create_market_data(key, symbol, name, data)

    return results


def fetch_asian_markets() -> dict:
    """
    Fetch Asian market indices.
    For these, we show: Gap at open + Intraday movement.
    """
    symbols = {
        'nikkei': ('^N225', 'Nikkei 225'),
        'hangseng': ('^HSI', 'Hang Seng'),
        'shanghai': ('000001.SS', 'Shanghai'),
        'kospi': ('^KS11', 'KOSPI'),
        'taiwan': ('^TWII', 'Taiwan'),
        'asx': ('^AXJO', 'ASX 200'),
    }

    results = {}
    for key, (symbol, name) in symbols.items():
        data = fetch_yahoo_quote_detailed(symbol)
        if data:
            results[key] = create_market_data(key, symbol, name, data)

    return results


def fetch_gift_nifty() -> Optional[dict]:
    """
    Fetch GIFT Nifty / SGX Nifty as indicator for Nifty opening.
    """
    # Try multiple symbols for GIFT Nifty
    symbols_to_try = [
        ('^NSEI', 'Nifty 50'),  # Reliable fallback when Yahoo does not expose a GIFT future symbol
    ]

    for symbol, name in symbols_to_try:
        data = fetch_yahoo_quote_detailed(symbol, quiet=True)
        if data and data['price'] > 0:
            return {
                'symbol': symbol,
                'name': name,
                'price': round(data['price'], 2),
                'prev_close': round(data['prev_close'], 2),
                'change': round(data['change'], 2),
                'change_pct': round(data['change_pct'], 2),
                'indicated_open': round(data['price'], 2),  # Current GIFT price indicates Nifty open
            }

    return None


def fetch_commodities() -> dict:
    """
    Fetch commodity futures (24-hour markets).
    Compare current vs yesterday's close for overnight move.
    """
    symbols = {
        'crude': ('CL=F', 'Crude Oil'),
        'brent': ('BZ=F', 'Brent Crude'),
        'gold': ('GC=F', 'Gold'),
        'silver': ('SI=F', 'Silver'),
        'natgas': ('NG=F', 'Natural Gas'),
        'copper': ('HG=F', 'Copper'),
    }

    results = {}
    for key, (symbol, name) in symbols.items():
        data = fetch_yahoo_quote_detailed(symbol, quiet=(key == 'copper'))
        if data:
            # Custom status for crude (inverted for India impact)
            if key in ('crude', 'brent'):
                def crude_status(pct):
                    if pct > 2: return "BEARISH_INDIA"
                    if pct > 1: return "CAUTIOUS_INDIA"
                    if pct < -2: return "BULLISH_INDIA"
                    if pct < -1: return "SUPPORTIVE_INDIA"
                    return "NEUTRAL"
                results[key] = create_market_data(key, symbol, name, data, crude_status)
            else:
                results[key] = create_market_data(key, symbol, name, data)

    return results


def fetch_currencies() -> dict:
    """
    Fetch currency pairs (24-hour markets).
    Compare current vs yesterday's close.
    """
    symbols = {
        'dxy': ('DX-Y.NYB', 'Dollar Index'),
        'usdinr': ('INR=X', 'USD/INR'),
        'eurusd': ('EURUSD=X', 'EUR/USD'),
        'gbpusd': ('GBPUSD=X', 'GBP/USD'),
        'usdjpy': ('JPY=X', 'USD/JPY'),
        'audusd': ('AUDUSD=X', 'AUD/USD'),
        'usdcnh': ('CNH=X', 'USD/CNH'),
    }

    results = {}
    for key, (symbol, name) in symbols.items():
        data = fetch_yahoo_quote_detailed(symbol)
        if data:
            # Custom status functions
            if key == 'dxy':
                def dxy_status(pct):
                    if pct > 0.5: return "STRONGLY_BEARISH_EM"
                    if pct > 0.2: return "BEARISH_EM"
                    if pct < -0.5: return "STRONGLY_BULLISH_EM"
                    if pct < -0.2: return "BULLISH_EM"
                    return "NEUTRAL"
                results[key] = create_market_data(key, symbol, name, data, dxy_status)

            elif key == 'usdinr':
                def inr_status(pct):
                    if pct > 0.3: return "INR_WEAK"
                    if pct < -0.3: return "INR_STRONG"
                    return "STABLE"
                results[key] = create_market_data(key, symbol, name, data, inr_status)
                # Fix decimal places for INR
                results[key].price = round(data['price'], 4)
                results[key].prev_close = round(data['prev_close'], 4)

            else:
                results[key] = create_market_data(key, symbol, name, data)
                # Fix decimal places for currency pairs
                results[key].price = round(data['price'], 4)
                results[key].prev_close = round(data['prev_close'], 4)

    return results


# =============================================================================
# SENTIMENT ANALYSIS
# =============================================================================

def analyze_sentiment(us_futures: dict, asian_markets: dict,
                      commodities: dict, currencies: dict) -> tuple:
    """
    Analyze overall market sentiment and generate signals.
    Returns: (risk_sentiment, dxy_impact, bullish_signals, bearish_signals, overall_bias)
    """
    bullish_signals = []
    bearish_signals = []
    risk_score = 0

    # US Futures Analysis
    for key, market in us_futures.items():
        if key == 'vix':
            if market.change_pct > 10:
                bearish_signals.append(f"VIX spiking {market.change_pct:+.1f}%")
                risk_score -= 2
            elif market.change_pct > 5:
                bearish_signals.append(f"VIX elevated {market.change_pct:+.1f}%")
                risk_score -= 1
            elif market.change_pct < -5:
                bullish_signals.append(f"VIX falling {market.change_pct:+.1f}%")
                risk_score += 1
        else:
            if market.change_pct > 0.5:
                bullish_signals.append(f"{market.name} {market.change_pct:+.1f}%")
                risk_score += 1
            elif market.change_pct < -0.5:
                bearish_signals.append(f"{market.name} {market.change_pct:+.1f}%")
                risk_score -= 1

    # Asian Markets Analysis
    asia_bullish = 0
    asia_bearish = 0
    for key, market in asian_markets.items():
        if market.change_pct > 0.5:
            asia_bullish += 1
        elif market.change_pct < -0.5:
            asia_bearish += 1

    if asia_bullish >= 3:
        bullish_signals.append(f"Asia broadly positive ({asia_bullish} markets up)")
        risk_score += 1
    elif asia_bearish >= 3:
        bearish_signals.append(f"Asia broadly negative ({asia_bearish} markets down)")
        risk_score -= 1

    # Commodity Analysis
    if 'crude' in commodities:
        crude = commodities['crude']
        if crude.change_pct > 2:
            bearish_signals.append(f"Crude surging {crude.change_pct:+.1f}% (negative for India)")
        elif crude.change_pct < -2:
            bullish_signals.append(f"Crude falling {crude.change_pct:+.1f}% (positive for India)")

    if 'gold' in commodities:
        gold = commodities['gold']
        if gold.change_pct > 1:
            # Gold up = risk-off signal
            bearish_signals.append(f"Gold rallying {gold.change_pct:+.1f}% (risk-off)")
            risk_score -= 1
        elif gold.change_pct < -1:
            bullish_signals.append(f"Gold weak {gold.change_pct:+.1f}% (risk-on)")
            risk_score += 1

    # Currency Analysis
    dxy_impact = "NEUTRAL"
    if 'dxy' in currencies:
        dxy = currencies['dxy']
        if dxy.change_pct > 0.5:
            bearish_signals.append(f"Dollar strong {dxy.change_pct:+.1f}% (bearish EM)")
            dxy_impact = "BEARISH_EM"
            risk_score -= 2
        elif dxy.change_pct > 0.2:
            dxy_impact = "MILDLY_BEARISH_EM"
            risk_score -= 1
        elif dxy.change_pct < -0.5:
            bullish_signals.append(f"Dollar weak {dxy.change_pct:+.1f}% (bullish EM)")
            dxy_impact = "BULLISH_EM"
            risk_score += 2
        elif dxy.change_pct < -0.2:
            dxy_impact = "MILDLY_BULLISH_EM"
            risk_score += 1

    # Risk-on/off currencies
    if 'audusd' in currencies:
        aud = currencies['audusd']
        if aud.change_pct > 0.5:
            risk_score += 1  # AUD up = risk-on
        elif aud.change_pct < -0.5:
            risk_score -= 1  # AUD down = risk-off

    if 'usdjpy' in currencies:
        jpy = currencies['usdjpy']
        if jpy.change_pct > 0.5:
            risk_score += 1  # Yen weakening = risk-on
        elif jpy.change_pct < -0.5:
            risk_score -= 1  # Yen strengthening = risk-off

    # Determine overall sentiment
    if risk_score >= 3:
        risk_sentiment = "STRONG_RISK_ON"
    elif risk_score >= 1:
        risk_sentiment = "RISK_ON"
    elif risk_score <= -3:
        risk_sentiment = "STRONG_RISK_OFF"
    elif risk_score <= -1:
        risk_sentiment = "RISK_OFF"
    else:
        risk_sentiment = "NEUTRAL"

    # Overall bias for Nifty
    if len(bullish_signals) > len(bearish_signals) + 2:
        overall_bias = "BULLISH"
    elif len(bearish_signals) > len(bullish_signals) + 2:
        overall_bias = "BEARISH"
    elif len(bullish_signals) > len(bearish_signals):
        overall_bias = "MILDLY BULLISH"
    elif len(bearish_signals) > len(bullish_signals):
        overall_bias = "MILDLY BEARISH"
    else:
        overall_bias = "NEUTRAL"

    return risk_sentiment, dxy_impact, bullish_signals, bearish_signals, overall_bias


# =============================================================================
# REPORT GENERATION
# =============================================================================

def fetch_all_markets() -> GlobalMarketsReport:
    """Fetch all global market data and generate report."""
    logger.info("Fetching US futures...")
    us_futures = fetch_us_futures()

    logger.info("Fetching Asian markets...")
    asian_markets = fetch_asian_markets()

    logger.info("Fetching GIFT Nifty...")
    gift_nifty = fetch_gift_nifty()

    logger.info("Fetching commodities...")
    commodities = fetch_commodities()

    logger.info("Fetching currencies...")
    currencies = fetch_currencies()

    # Analyze sentiment
    risk_sentiment, dxy_impact, bullish, bearish, overall = analyze_sentiment(
        us_futures, asian_markets, commodities, currencies
    )

    return GlobalMarketsReport(
        timestamp=datetime.now().isoformat(),
        us_futures={k: asdict(v) for k, v in us_futures.items()},
        asian_markets={k: asdict(v) for k, v in asian_markets.items()},
        commodities={k: asdict(v) for k, v in commodities.items()},
        currencies={k: asdict(v) for k, v in currencies.items()},
        gift_nifty=gift_nifty,
        risk_sentiment=risk_sentiment,
        dxy_impact=dxy_impact,
        bullish_signals=bullish,
        bearish_signals=bearish,
        overall_bias=overall
    )


def format_market_line(data: dict, show_gap: bool = False, decimals: int = 2) -> str:
    """Format a single market data line."""
    price_fmt = f"{data['price']:>10,.{decimals}f}"
    change_fmt = f"{data['change_pct']:>+6.2f}%"

    if show_gap and (data['gap_pct'] != 0 or data['intraday_pct'] != 0):
        gap_fmt = f"Gap:{data['gap_pct']:>+5.1f}%"
        intra_fmt = f"Intra:{data['intraday_pct']:>+5.1f}%"
        return f"  {data['name']:<18} {price_fmt} {change_fmt}  ({gap_fmt} {intra_fmt}) [{data['status']}]"
    else:
        status = data.get('market_status', '')
        status_str = f" ({status})" if status and status != 'CLOSED' else ""
        return f"  {data['name']:<18} {price_fmt} {change_fmt}  [{data['status']}]{status_str}"


def format_report(report: GlobalMarketsReport) -> str:
    """Format report as readable text."""
    lines = []
    lines.append("=" * 80)
    lines.append(f"GLOBAL MARKETS OVERNIGHT REPORT - {datetime.now().strftime('%B %d, %Y %H:%M IST')}")
    lines.append("=" * 80)
    lines.append("")

    # GIFT Nifty / Nifty Indication
    if report.gift_nifty:
        lines.append("NIFTY INDICATION")
        lines.append("-" * 50)
        gn = report.gift_nifty
        lines.append(f"  {gn['name']:<18} {gn['price']:>10,.2f}  {gn['change_pct']:>+6.2f}%")
        lines.append(f"  Indicated Nifty Open: ~{gn['indicated_open']:,.0f} ({gn['change_pct']:>+.2f}% from prev close)")
        lines.append("")

    # US Futures
    lines.append("US FUTURES (Overnight vs Yesterday Close)")
    lines.append("-" * 50)
    for key, data in report.us_futures.items():
        lines.append(format_market_line(data))
    lines.append("")

    # Asian Markets
    lines.append("ASIAN MARKETS (Gap at Open + Intraday Move)")
    lines.append("-" * 50)
    for key, data in report.asian_markets.items():
        lines.append(format_market_line(data, show_gap=True))
    lines.append("")

    # Commodities
    lines.append("COMMODITIES (Overnight Move)")
    lines.append("-" * 50)
    for key, data in report.commodities.items():
        lines.append(format_market_line(data))
    lines.append("")

    # Currencies
    lines.append("CURRENCIES (Overnight Move)")
    lines.append("-" * 50)
    for key, data in report.currencies.items():
        decimals = 4 if 'usd' in key.lower() or key == 'usdinr' else 2
        lines.append(format_market_line(data, decimals=decimals))
    lines.append("")

    # Summary
    lines.append("=" * 80)
    lines.append("SENTIMENT ANALYSIS")
    lines.append("=" * 80)
    lines.append(f"  Risk Sentiment: {report.risk_sentiment}")
    lines.append(f"  DXY Impact: {report.dxy_impact}")
    lines.append("")

    lines.append(f"  BULLISH SIGNALS ({len(report.bullish_signals)}):")
    if report.bullish_signals:
        for signal in report.bullish_signals:
            lines.append(f"    + {signal}")
    else:
        lines.append("    (none)")
    lines.append("")

    lines.append(f"  BEARISH SIGNALS ({len(report.bearish_signals)}):")
    if report.bearish_signals:
        for signal in report.bearish_signals:
            lines.append(f"    - {signal}")
    else:
        lines.append("    (none)")
    lines.append("")

    lines.append("-" * 50)
    lines.append(f"  OVERALL BIAS FOR NIFTY: {report.overall_bias}")
    lines.append("=" * 80)

    return "\n".join(lines)


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description='Global markets analysis')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    args = parser.parse_args()

    try:
        report = fetch_all_markets()

        if args.json:
            print(json.dumps(asdict(report), indent=2))
        else:
            print(format_report(report))

        return 0

    except Exception as e:
        logger.error(f"Failed to fetch global markets: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
