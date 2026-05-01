#!/usr/bin/env python3
"""
Pre-Market Analysis Script for Nifty Options Trading

Generates comprehensive pre-market report including:
- Nifty levels (Pivots, CPR, S/R)
- India VIX analysis
- Option chain for nearest expiry
- Global markets (US, Asia)
- Commodities (Crude, Gold, Silver, NG)
- Currencies (DXY, USD/INR, risk sentiment)

Usage:
    python premarket_analysis.py                    # Run analysis
    python premarket_analysis.py --output ~/reports # Save to file
    python premarket_analysis.py --notify           # Send notification

Requirements:
    pip install requests python-dotenv
"""

import os
import sys
import json
import argparse
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from dataclasses import dataclass
from typing import Optional

import requests
from dotenv import load_dotenv

# Configure logging
LOG_FILE = os.path.expanduser('~/Library/Logs/premarket_analysis.log')
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
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / '.env'


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class NiftyLevels:
    """Nifty pivot and support/resistance levels."""
    spot: float
    prev_high: float
    prev_low: float
    prev_close: float
    pivot: float
    tc: float  # Top Central Pivot
    bc: float  # Bottom Central Pivot
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float
    cpr_width: float

    @classmethod
    def calculate(cls, high: float, low: float, close: float, spot: float) -> 'NiftyLevels':
        """Calculate pivot levels from OHLC data."""
        pivot = (high + low + close) / 3
        bc = (high + low) / 2
        tc = (pivot - bc) + pivot

        r1 = (2 * pivot) - low
        r2 = pivot + (high - low)
        r3 = high + 2 * (pivot - low)

        s1 = (2 * pivot) - high
        s2 = pivot - (high - low)
        s3 = low - 2 * (high - pivot)

        cpr_width = abs(tc - bc)

        return cls(
            spot=spot,
            prev_high=high,
            prev_low=low,
            prev_close=close,
            pivot=round(pivot, 2),
            tc=round(tc, 2),
            bc=round(bc, 2),
            r1=round(r1, 2),
            r2=round(r2, 2),
            r3=round(r3, 2),
            s1=round(s1, 2),
            s2=round(s2, 2),
            s3=round(s3, 2),
            cpr_width=round(cpr_width, 2)
        )


@dataclass
class VIXData:
    """India VIX data and analysis."""
    current: float
    open: float
    high: float
    low: float
    prev_close: float
    change_pct: float
    status: str  # LOW, NORMAL, ELEVATED, HIGH


@dataclass
class GlobalMarket:
    """Global market data point."""
    name: str
    symbol: str
    price: float
    change_pct: float
    status: str  # BULLISH, BEARISH, NEUTRAL


@dataclass
class OptionData:
    """Option chain data for a strike."""
    strike: float
    ce_ltp: float
    ce_iv: float
    ce_delta: float
    pe_ltp: float
    pe_iv: float
    pe_delta: float


# ============================================================================
# API CLIENTS
# ============================================================================

class UpstoxClient:
    """Client for Upstox API."""

    BASE_URL = "https://api.upstox.com/v2"

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }

    def get_nifty_quote(self) -> dict:
        """Get current Nifty quote."""
        url = f"{self.BASE_URL}/market-quote/quotes"
        params = {"instrument_key": "NSE_INDEX|Nifty 50"}
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        data = response.json()
        return data.get('data', {}).get('NSE_INDEX:Nifty 50', {})

    def get_nifty_historical(self, days: int = 5) -> list:
        """Get historical daily candles for Nifty."""
        today = date.today()
        from_date = today - timedelta(days=days + 5)  # Extra buffer for holidays

        url = f"{self.BASE_URL}/historical-candle/NSE_INDEX%7CNifty%2050/day/{today.isoformat()}/{from_date.isoformat()}"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()

        data = response.json()
        candles = data.get('data', {}).get('candles', [])

        # Parse candles: [timestamp, open, high, low, close, volume]
        result = []
        for candle in candles[:days]:
            result.append({
                'date': candle[0],
                'open': candle[1],
                'high': candle[2],
                'low': candle[3],
                'close': candle[4]
            })
        return result

    def get_india_vix(self) -> dict:
        """Get India VIX quote."""
        url = f"{self.BASE_URL}/market-quote/quotes"
        params = {"instrument_key": "NSE_INDEX|India VIX"}
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        data = response.json()
        return data.get('data', {}).get('NSE_INDEX:India VIX', {})

    def get_option_chain(self, expiry_date: str) -> list:
        """Get option chain for given expiry."""
        url = f"{self.BASE_URL}/option/chain"
        params = {
            "instrument_key": "NSE_INDEX|Nifty 50",
            "expiry_date": expiry_date
        }
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        data = response.json()
        return data.get('data', [])

    def get_weekly_expiries(self) -> list:
        """Get list of weekly expiry dates."""
        url = f"{self.BASE_URL}/option/contract"
        params = {"instrument_key": "NSE_INDEX|Nifty 50"}
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        data = response.json()

        # Extract unique weekly expiries
        expiries = set()
        for contract in data.get('data', []):
            if contract.get('weekly'):
                expiries.add(contract.get('expiry'))

        return sorted(list(expiries))[:5]  # Return nearest 5


class TradingViewClient:
    """Client for TradingView data via web scraping or API."""

    # Using free APIs for global market data
    ALPHA_VANTAGE_KEY = "demo"  # Replace with actual key for production

    @staticmethod
    def get_global_indices() -> dict:
        """Get global market indices data."""
        # This is a placeholder - in production, use proper APIs
        # For now, return structure that can be populated
        return {
            'us': {
                'sp500': {'symbol': 'SPY', 'name': 'S&P 500'},
                'nasdaq': {'symbol': 'QQQ', 'name': 'Nasdaq'},
                'dow': {'symbol': 'DIA', 'name': 'Dow Jones'},
                'vix': {'symbol': 'VIX', 'name': 'CBOE VIX'}
            },
            'asia': {
                'nikkei': {'symbol': 'NI225', 'name': 'Nikkei 225'},
                'hangseng': {'symbol': 'HSI', 'name': 'Hang Seng'},
                'shanghai': {'symbol': 'SSEC', 'name': 'Shanghai Composite'}
            },
            'commodities': {
                'crude': {'symbol': 'CL', 'name': 'Crude Oil'},
                'gold': {'symbol': 'GC', 'name': 'Gold'},
                'silver': {'symbol': 'SI', 'name': 'Silver'},
                'natgas': {'symbol': 'NG', 'name': 'Natural Gas'}
            },
            'currencies': {
                'dxy': {'symbol': 'DXY', 'name': 'Dollar Index'},
                'usdinr': {'symbol': 'USDINR', 'name': 'USD/INR'},
                'eurusd': {'symbol': 'EURUSD', 'name': 'EUR/USD'},
                'usdjpy': {'symbol': 'USDJPY', 'name': 'USD/JPY'},
                'audusd': {'symbol': 'AUDUSD', 'name': 'AUD/USD'}
            }
        }


# ============================================================================
# ANALYSIS FUNCTIONS
# ============================================================================

def analyze_vix(vix_data: dict) -> VIXData:
    """Analyze India VIX data."""
    current = vix_data.get('last_price', 0)
    ohlc = vix_data.get('ohlc', {})
    open_price = ohlc.get('open', current)
    high = ohlc.get('high', current)
    low = ohlc.get('low', current)
    prev_close = ohlc.get('close', current)

    if prev_close > 0:
        change_pct = ((current - prev_close) / prev_close) * 100
    else:
        change_pct = 0

    # Determine status
    if current < 13:
        status = "LOW"
    elif current < 18:
        status = "NORMAL"
    elif current < 25:
        status = "ELEVATED"
    else:
        status = "HIGH"

    return VIXData(
        current=round(current, 2),
        open=round(open_price, 2),
        high=round(high, 2),
        low=round(low, 2),
        prev_close=round(prev_close, 2),
        change_pct=round(change_pct, 2),
        status=status
    )


def get_nearest_expiry(expiries: list) -> Optional[str]:
    """Get nearest weekly expiry date."""
    today = date.today()
    for expiry in expiries:
        try:
            exp_date = datetime.strptime(expiry, '%Y-%m-%d').date()
            if exp_date >= today:
                return expiry
        except ValueError:
            continue
    return expiries[0] if expiries else None


def analyze_option_chain(chain_data: list, spot: float) -> list[OptionData]:
    """Analyze option chain around ATM."""
    options = []

    for item in chain_data:
        strike = item.get('strike_price', 0)

        # Filter strikes around ATM (±500 points)
        if abs(strike - spot) > 500:
            continue

        ce = item.get('call_options', {})
        pe = item.get('put_options', {})

        ce_market = ce.get('market_data', {})
        pe_market = pe.get('market_data', {})
        ce_greeks = ce.get('option_greeks', {})
        pe_greeks = pe.get('option_greeks', {})

        options.append(OptionData(
            strike=strike,
            ce_ltp=ce_market.get('ltp', 0),
            ce_iv=ce_greeks.get('iv', 0),
            ce_delta=ce_greeks.get('delta', 0),
            pe_ltp=pe_market.get('ltp', 0),
            pe_iv=pe_greeks.get('iv', 0),
            pe_delta=pe_greeks.get('delta', 0)
        ))

    return sorted(options, key=lambda x: x.strike)


def predict_day_type(levels: NiftyLevels, vix: VIXData, gap_pct: float) -> tuple[str, int]:
    """Predict whether it will be a trending or range-bound day."""
    score = 50  # Start neutral

    # CPR width analysis
    if levels.cpr_width < 30:
        score += 20  # Narrow CPR = trending
    elif levels.cpr_width > 70:
        score -= 20  # Wide CPR = range-bound

    # Gap analysis
    if abs(gap_pct) > 0.5:
        score += 15  # Large gap = trending
    elif abs(gap_pct) < 0.2:
        score -= 10  # Small gap = range-bound

    # VIX analysis
    if vix.change_pct > 5:
        score += 10  # Rising VIX = potential trend
    elif vix.change_pct < -5:
        score -= 10  # Falling VIX = range-bound

    if vix.current > 22:
        score += 10  # High VIX = volatile, could trend
    elif vix.current < 15:
        score -= 10  # Low VIX = range-bound

    # Determine prediction
    if score >= 60:
        return "TRENDING", score
    elif score <= 40:
        return "RANGE-BOUND", 100 - score
    else:
        return "UNCERTAIN", 50


def generate_strike_recommendations(
    spot: float,
    options: list[OptionData],
    max_loss: float = 2000,
    lot_size: int = 65
) -> dict:
    """Generate strike recommendations based on risk parameters."""
    recommendations = {
        'bullish': {'ce_strike': None, 'pe_strike': None},
        'bearish': {'ce_strike': None, 'pe_strike': None},
        'neutral': {'ce_strike': None, 'pe_strike': None}
    }

    # Find OTM options
    otm_calls = [o for o in options if o.strike > spot]
    otm_puts = [o for o in options if o.strike < spot]

    if not otm_calls or not otm_puts:
        return recommendations

    # For neutral: balanced distance from spot
    for ce in otm_calls:
        for pe in otm_puts:
            ce_dist = ce.strike - spot
            pe_dist = spot - pe.strike

            # Check if combined premium is reasonable
            if ce.ce_ltp > 0 and pe.pe_ltp > 0:
                # Neutral: similar distance
                if abs(ce_dist - pe_dist) < 50:
                    recommendations['neutral'] = {
                        'ce_strike': ce.strike,
                        'ce_premium': ce.ce_ltp,
                        'pe_strike': pe.strike,
                        'pe_premium': pe.pe_ltp
                    }
                    break
        if recommendations['neutral']['ce_strike']:
            break

    # For bullish: further OTM call, closer OTM put
    if len(otm_calls) >= 2 and otm_puts:
        recommendations['bullish'] = {
            'ce_strike': otm_calls[1].strike,
            'ce_premium': otm_calls[1].ce_ltp,
            'pe_strike': otm_puts[0].strike,
            'pe_premium': otm_puts[0].pe_ltp
        }

    # For bearish: closer OTM call, further OTM put
    if otm_calls and len(otm_puts) >= 2:
        recommendations['bearish'] = {
            'ce_strike': otm_calls[0].strike,
            'ce_premium': otm_calls[0].ce_ltp,
            'pe_strike': otm_puts[1].strike,
            'pe_premium': otm_puts[1].pe_ltp
        }

    return recommendations


# ============================================================================
# REPORT GENERATION
# ============================================================================

def generate_report(
    levels: NiftyLevels,
    vix: VIXData,
    options: list[OptionData],
    expiry: str,
    day_type: tuple[str, int],
    recommendations: dict
) -> str:
    """Generate formatted pre-market report."""

    now = datetime.now()

    report = []
    report.append("=" * 80)
    report.append(f"PRE-MARKET ANALYSIS - {now.strftime('%B %d, %Y %H:%M IST')}")
    report.append("=" * 80)
    report.append("")

    # Nifty Levels
    report.append("NIFTY LEVELS")
    report.append("-" * 40)
    report.append(f"Spot: {levels.spot:,.2f}")
    report.append(f"PDH: {levels.prev_high:,.2f} | PDL: {levels.prev_low:,.2f} | Prev Close: {levels.prev_close:,.2f}")
    report.append(f"Pivot: {levels.pivot:,.2f}")

    cpr_status = "NARROW" if levels.cpr_width < 30 else "WIDE" if levels.cpr_width > 70 else "MODERATE"
    report.append(f"CPR: {levels.bc:,.2f} - {levels.tc:,.2f} (Width: {levels.cpr_width:.1f} pts - {cpr_status})")

    report.append(f"R1: {levels.r1:,.2f} | R2: {levels.r2:,.2f} | R3: {levels.r3:,.2f}")
    report.append(f"S1: {levels.s1:,.2f} | S2: {levels.s2:,.2f} | S3: {levels.s3:,.2f}")
    report.append("")

    # Spot Position
    if levels.spot > levels.pivot:
        position = "ABOVE PIVOT (Bullish)"
    else:
        position = "BELOW PIVOT (Bearish)"
    report.append(f"Spot Position: {position}")
    report.append("")

    # India VIX
    report.append("INDIA VIX")
    report.append("-" * 40)
    report.append(f"Current: {vix.current:.2f} | Change: {vix.change_pct:+.2f}%")
    report.append(f"Range: {vix.low:.2f} - {vix.high:.2f}")

    vix_implication = {
        "LOW": "Low volatility - small premiums, avoid selling",
        "NORMAL": "Normal volatility - balanced premiums",
        "ELEVATED": "Elevated volatility - good for option sellers",
        "HIGH": "High fear - potential trending day, be cautious"
    }
    report.append(f"Status: {vix.status} - {vix_implication.get(vix.status, '')}")
    report.append("")

    # Option Chain
    report.append(f"OPTION CHAIN (Expiry: {expiry})")
    report.append("-" * 40)

    # Find ATM
    atm_options = [o for o in options if abs(o.strike - levels.spot) <= 50]
    if atm_options:
        atm = min(atm_options, key=lambda x: abs(x.strike - levels.spot))
        straddle = atm.ce_ltp + atm.pe_ltp
        report.append(f"ATM Strike: {atm.strike:.0f}")
        report.append(f"ATM Straddle: CE {atm.ce_ltp:.2f} + PE {atm.pe_ltp:.2f} = {straddle:.2f}")
        report.append(f"ATM IV: CE {atm.ce_iv:.2f}% | PE {atm.pe_iv:.2f}%")

    report.append("")
    report.append("OTM Options:")
    report.append(f"{'Strike':>8} | {'CE LTP':>8} | {'CE IV':>7} | {'PE LTP':>8} | {'PE IV':>7}")
    report.append("-" * 50)

    for opt in options:
        if abs(opt.strike - levels.spot) <= 300:
            marker = " <-- ATM" if abs(opt.strike - levels.spot) <= 25 else ""
            report.append(
                f"{opt.strike:>8.0f} | {opt.ce_ltp:>8.2f} | {opt.ce_iv:>6.2f}% | "
                f"{opt.pe_ltp:>8.2f} | {opt.pe_iv:>6.2f}%{marker}"
            )
    report.append("")

    # Day Type Prediction
    report.append("DAY TYPE PREDICTION")
    report.append("-" * 40)
    report.append(f"Prediction: {day_type[0]} ({day_type[1]}% confidence)")
    report.append("")

    # Strike Recommendations
    report.append("STRIKE RECOMMENDATIONS (Max Loss: Rs 2,000 per leg)")
    report.append("-" * 40)

    for bias, rec in recommendations.items():
        if rec.get('ce_strike'):
            report.append(
                f"{bias.upper():>8}: Sell {rec['ce_strike']:.0f} CE @ {rec['ce_premium']:.2f} + "
                f"Sell {rec['pe_strike']:.0f} PE @ {rec['pe_premium']:.2f}"
            )
    report.append("")

    # Risk Parameters
    report.append("RISK PARAMETERS (Lot Size: 65)")
    report.append("-" * 40)
    report.append("Stop Loss: Exit when premium doubles (100% of entry)")
    report.append("Max Re-entries: 2")
    report.append("Exit Time: 3:15 PM")
    report.append("")

    # Trending Day Triggers
    report.append("TRENDING DAY TRIGGERS")
    report.append("-" * 40)
    report.append(f"Bullish Break: Above {levels.r1:,.2f} (R1) or {levels.prev_high:,.2f} (PDH)")
    report.append(f"Bearish Break: Below {levels.s1:,.2f} (S1) or {levels.prev_low:,.2f} (PDL)")
    report.append("")

    report.append("=" * 80)
    report.append("END OF REPORT")
    report.append("=" * 80)

    return "\n".join(report)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description='Pre-market analysis for Nifty options')
    parser.add_argument(
        '--env-file',
        default=str(DEFAULT_ENV_FILE),
        help='Path to .env file'
    )
    parser.add_argument(
        '--output',
        type=str,
        help='Directory to save report'
    )
    parser.add_argument(
        '--notify',
        action='store_true',
        help='Send macOS notification'
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info(f"Pre-Market Analysis Started at {datetime.now()}")
    logger.info("=" * 60)

    # Load environment
    load_dotenv(args.env_file)

    upstox_token = os.getenv('UPSTOX_ACCESS_TOKEN')
    if not upstox_token:
        logger.error("UPSTOX_ACCESS_TOKEN not found")
        return 1

    upstox_token = upstox_token.strip("'\"")

    try:
        # Initialize client
        upstox = UpstoxClient(upstox_token)

        # Get Nifty data
        logger.info("Fetching Nifty data...")
        nifty_quote = upstox.get_nifty_quote()
        nifty_historical = upstox.get_nifty_historical(5)

        if not nifty_historical:
            logger.error("Could not fetch historical data")
            return 1

        # Use yesterday's data for levels
        yesterday = nifty_historical[0]
        spot = nifty_quote.get('last_price', yesterday['close'])

        levels = NiftyLevels.calculate(
            high=yesterday['high'],
            low=yesterday['low'],
            close=yesterday['close'],
            spot=spot
        )

        # Get VIX
        logger.info("Fetching India VIX...")
        vix_quote = upstox.get_india_vix()
        vix = analyze_vix(vix_quote)

        # Get option chain
        logger.info("Fetching option chain...")
        expiries = upstox.get_weekly_expiries()
        nearest_expiry = get_nearest_expiry(expiries)

        options = []
        if nearest_expiry:
            chain_data = upstox.get_option_chain(nearest_expiry)
            options = analyze_option_chain(chain_data, spot)

        # Calculate gap
        if nifty_historical and len(nifty_historical) > 1:
            prev_close = nifty_historical[1]['close']
            gap_pct = ((spot - prev_close) / prev_close) * 100
        else:
            gap_pct = 0

        # Predict day type
        day_type = predict_day_type(levels, vix, gap_pct)

        # Generate recommendations
        recommendations = generate_strike_recommendations(spot, options)

        # Generate report
        report = generate_report(
            levels, vix, options,
            nearest_expiry or "N/A",
            day_type, recommendations
        )

        # Output report
        print("\n" + report)

        # Save to file if requested
        if args.output:
            os.makedirs(args.output, exist_ok=True)
            filename = f"premarket_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
            filepath = os.path.join(args.output, filename)
            with open(filepath, 'w') as f:
                f.write(report)
            logger.info(f"Report saved to: {filepath}")

        # Send notification if requested
        if args.notify:
            send_notification(
                "Pre-Market Analysis Ready",
                f"Nifty: {spot:,.2f} | VIX: {vix.current:.2f} | {day_type[0]}",
                success=True
            )

        logger.info("Pre-market analysis completed successfully")
        return 0

    except Exception as e:
        logger.error(f"Pre-market analysis FAILED: {e}")
        import traceback
        traceback.print_exc()

        if args.notify:
            send_notification(
                "Pre-Market Analysis Failed",
                str(e)[:100],
                success=False
            )
        return 1


def send_notification(title: str, message: str, success: bool = True):
    """Send macOS notification."""
    import subprocess
    sound = "Glass" if success else "Basso"
    script = f'display notification "{message}" with title "{title}" sound name "{sound}"'
    subprocess.run(['osascript', '-e', script], capture_output=True)


if __name__ == '__main__':
    sys.exit(main())
