#!/usr/bin/env python3
"""
Trade Journaling Automation Script

Fetches executed trades from Upstox, calculates P&L and fees,
splits into tranches if needed, and populates Notion Trading Journal.

Usage:
    python trade_journaling.py                    # Process today's trades
    python trade_journaling.py --date 2026-04-13  # Process specific date
    python trade_journaling.py --dry-run          # Preview without creating entries

Requirements:
    pip install requests python-dotenv
"""

import os
import sys
import json
import argparse
import logging
import gzip
import io
from datetime import datetime, date, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from collections import defaultdict

import re
import requests
from dotenv import load_dotenv

from journal_keys import (
    JOURNAL_KEY_PROPERTY,
    build_journal_key,
    extract_source_ids,
)

# Configure logging
LOG_FILE = os.path.expanduser('~/Library/Logs/trade_journaling.log')
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
# CONTRACT SPECIFICATIONS
# ============================================================================
# MCX Commodity Futures contract multipliers (units per lot)
# These are the actual multipliers for P&L calculation
CONTRACT_SPECS = {
    # Natural Gas
    'NATGAS': {'multiplier': 1250, 'tick_size': 0.10, 'name': 'Natural Gas'},
    'NATGASMINI': {'multiplier': 250, 'tick_size': 0.10, 'name': 'Natural Gas Mini'},

    # Crude Oil
    'CRUDEOIL': {'multiplier': 100, 'tick_size': 1.00, 'name': 'Crude Oil'},
    'CRUDEOILM': {'multiplier': 10, 'tick_size': 1.00, 'name': 'Crude Oil Mini'},

    # Gold
    'GOLD': {'multiplier': 100, 'tick_size': 1.00, 'name': 'Gold'},
    'GOLDM': {'multiplier': 10, 'tick_size': 1.00, 'name': 'Gold Mini'},
    'GOLDGUINEA': {'multiplier': 1, 'tick_size': 1.00, 'name': 'Gold Guinea'},
    'GOLDPETAL': {'multiplier': 1, 'tick_size': 1.00, 'name': 'Gold Petal'},

    # Silver
    'SILVER': {'multiplier': 30, 'tick_size': 1.00, 'name': 'Silver'},  # 30 kg
    'SILVERM': {'multiplier': 5, 'tick_size': 1.00, 'name': 'Silver Mini'},  # 5 kg
    'SILVERMIC': {'multiplier': 1, 'tick_size': 1.00, 'name': 'Silver Micro'},  # 1 kg

    # Base Metals
    'COPPER': {'multiplier': 2500, 'tick_size': 0.05, 'name': 'Copper'},
    'ZINC': {'multiplier': 5000, 'tick_size': 0.05, 'name': 'Zinc'},
    'ZINCMINI': {'multiplier': 1000, 'tick_size': 0.05, 'name': 'Zinc Mini'},
    'LEAD': {'multiplier': 5000, 'tick_size': 0.05, 'name': 'Lead'},
    'LEADMINI': {'multiplier': 1000, 'tick_size': 0.05, 'name': 'Lead Mini'},
    'ALUMINIUM': {'multiplier': 5000, 'tick_size': 0.05, 'name': 'Aluminium'},
    'ALUMINI': {'multiplier': 1000, 'tick_size': 0.05, 'name': 'Aluminium Mini'},
    'NICKEL': {'multiplier': 1500, 'tick_size': 0.10, 'name': 'Nickel'},

    # Agricultural
    'COTTON': {'multiplier': 25, 'tick_size': 10.00, 'name': 'Cotton'},
    'MENTHAOIL': {'multiplier': 360, 'tick_size': 0.10, 'name': 'Mentha Oil'},
}


INDEX_SYMBOLS = {
    'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50',
    'SENSEX', 'BANKEX'
}


@dataclass
class ParsedInstrument:
    """Parsed instrument details from trading symbol."""
    base_symbol: str
    expiry_date: Optional[date]
    instrument_type: str  # FUT, CE, PE
    strike: Optional[float] = None

    @property
    def is_option(self) -> bool:
        return self.instrument_type in ('CE', 'PE')

    @property
    def option_type(self) -> Optional[str]:
        """Returns 'Call' or 'Put' for options, None for futures."""
        if self.instrument_type == 'CE':
            return 'Call'
        elif self.instrument_type == 'PE':
            return 'Put'
        return None


@dataclass
class Order:
    """Represents a single executed order."""
    order_id: str
    trading_symbol: str
    transaction_type: str  # BUY or SELL
    quantity: int
    average_price: float
    order_timestamp: datetime
    exchange: str
    instrument_token: Optional[str] = None
    time_text: Optional[str] = None
    trade_id: Optional[str] = None


@dataclass
class TradeEntry:
    """Represents a single journal entry (one tranche of a trade)."""
    symbol: str
    direction: str  # Long or Short
    entry_date: date
    entry_time: str
    entry_price: float
    quantity: int
    instrument_type: str
    lot_size: int  # Contract multiplier
    fees: float
    # Exit fields - None for open positions
    exit_date: Optional[date] = None
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    stop_loss: Optional[float] = None
    target: Optional[float] = None
    pnl: float = 0.0
    outcome: str = "Open"  # Win, Loss, Breakeven, Open
    expiry_date: Optional[date] = None
    timeframe: str = "Intraday"  # Intraday or Positional
    option_type: Optional[str] = None  # Call, Put, or None for futures
    option_strike: Optional[float] = None
    raw_quantity: Optional[int] = None
    status: str = "Open"  # Open, Partially Filled, Closed
    notion_page_id: Optional[str] = None  # For updating existing entries
    strategy: Optional[str] = None
    pre_trade_notes: Optional[str] = None
    post_trade_review: Optional[str] = None
    followed_plan: Optional[bool] = None
    entry_source_ids: list[str] = field(default_factory=list)
    exit_source_ids: list[str] = field(default_factory=list)
    journal_key: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.exit_date is None


@dataclass
class OpenPosition:
    """Represents an open position from Notion."""
    page_id: str
    symbol: str
    direction: str
    entry_date: date
    entry_time: str
    entry_price: float
    quantity: int
    instrument_type: str
    lot_size: int
    fees: float
    stop_loss: Optional[float]
    expiry_date: Optional[date]
    option_type: Optional[str]
    option_strike: Optional[float]
    status: str  # Open or Partially Filled
    raw_quantity: Optional[int] = None
    label: str = ""
    journal_key: Optional[str] = None


class UpstoxClient:
    """Client for Upstox API interactions."""

    BASE_URL = "https://api.upstox.com/v2"
    INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange"

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }
        self._instruments_cache = {}

    def get_completed_orders(self, target_date: Optional[date] = None) -> list[Order]:
        """Fetch completed trades for the requested date.

        Upstox uses different APIs for:
        - current day trades
        - historical trade backfills
        """
        if target_date is None or target_date == date.today():
            return self._get_trades_for_day(target_date)
        return self._get_historical_trades_for_date(target_date)

    @staticmethod
    def _safe_time_text(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        text = value.strip()
        if not text:
            return None
        for fmt in (
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%dT%H:%M:%S%z',
            '%Y-%m-%dT%H:%M:%S.%f%z',
            '%H:%M:%S',
            '%H:%M',
        ):
            try:
                return datetime.strptime(text, fmt).strftime('%H:%M')
            except ValueError:
                continue
        match = re.search(r'(\d{2}:\d{2})(?::\d{2})?', text)
        if match:
            return match.group(1)
        return None

    def _get_trades_for_day(self, target_date: Optional[date] = None) -> list[Order]:
        """Fetch executed trades for the current day from Upstox trade-history API."""
        url = f"{self.BASE_URL}/order/trades/get-trades-for-day"
        response = requests.get(url, headers=self.headers, timeout=20)
        response.raise_for_status()

        data = response.json()
        if data.get('status') != 'success':
            raise Exception(f"API error: {data}")

        orders = []
        for item in data.get('data', []):
            # The trades-for-day endpoint already returns executed trades.
            # Some payloads do not include a "status" field at all, so only
            # skip rows when an explicit non-complete status is present.
            status = (item.get('status') or '').lower().strip()
            if status and status != 'complete':
                continue

            order_timestamp = datetime.strptime(
                item['order_timestamp'],
                '%Y-%m-%d %H:%M:%S'
            )
            if target_date is not None and order_timestamp.date() != target_date:
                continue
            orders.append(Order(
                order_id=item['order_id'],
                trading_symbol=item.get('trading_symbol') or item.get('tradingsymbol', ''),
                transaction_type=item['transaction_type'],
                quantity=item['quantity'],
                average_price=item['average_price'],
                order_timestamp=order_timestamp,
                exchange=item['exchange'],
                instrument_token=item.get('instrument_token'),
                # For same-day trade journaling, Upstox order book time aligns with
                # order_timestamp. In live testing, exchange_timestamp can be shifted
                # by +05:30 for some MCX rows, which leads to wrong Notion times.
                time_text=self._safe_time_text(item.get('order_timestamp') or item.get('exchange_timestamp')),
                trade_id=item.get('trade_id') or item.get('order_id'),
            ))

        return orders

    def _get_historical_trades_for_date(self, target_date: date) -> list[Order]:
        """Fetch historical trades for a past date using Upstox historical trades API.

        Official doc:
        https://upstox.com/developer/api-documentation/get-historical-trades/
        """
        url = f"{self.BASE_URL}/charges/historical-trades"
        segments = ['EQ', 'FO', 'COM', 'CD']
        orders: list[Order] = []

        for segment in segments:
            page_number = 1
            while True:
                params = {
                    'segment': segment,
                    'start_date': target_date.isoformat(),
                    'end_date': target_date.isoformat(),
                    'page_number': page_number,
                    'page_size': 5000,
                }
                response = requests.get(url, headers=self.headers, params=params, timeout=20)
                response.raise_for_status()

                data = response.json()
                if data.get('status') != 'success':
                    raise Exception(f"Historical trades API error: {data}")

                records = data.get('data', [])
                for idx, item in enumerate(records):
                    orders.append(self._historical_trade_to_order(item, segment, idx))

                page = data.get('meta_data', {}).get('page', {})
                total_pages = int(page.get('total_pages') or 1)
                if page_number >= total_pages:
                    break
                page_number += 1

        return orders

    def _historical_trade_to_order(self, item: dict, segment: str, index: int) -> Order:
        trade_date = date.fromisoformat(item['trade_date'])
        trading_symbol = self._build_historical_trading_symbol(item, segment)
        order_id = item.get('trade_id') or f"HIST-{segment}-{trade_date.isoformat()}-{index}"
        exchange = item.get('exchange', '')
        instrument_token = item.get('instrument_token') or None
        time_text = self._safe_time_text(
            item.get('exchange_timestamp')
            or item.get('trade_timestamp')
            or item.get('order_timestamp')
            or item.get('trade_time')
            or item.get('timestamp')
        )

        return Order(
            order_id=order_id,
            trading_symbol=trading_symbol,
            transaction_type=item['transaction_type'],
            quantity=int(item['quantity']),
            average_price=float(item['price']),
            order_timestamp=datetime.combine(trade_date, time.min),
            exchange=exchange,
            instrument_token=instrument_token,
            time_text=time_text,
            trade_id=item.get('trade_id') or order_id,
        )

    def _build_historical_trading_symbol(self, item: dict, segment: str) -> str:
        """Create an internal trading symbol string that our parser can understand.

        Historical trades do not always return the compact Upstox trading symbol,
        especially for COM and some FO rows, so we reconstruct a parseable form.
        """
        base_symbol = (item.get('symbol') or item.get('scrip_name') or '').replace(' ', '').upper()
        option_type = (item.get('option_type') or '').upper()
        expiry = item.get('expiry') or ''
        strike_price = item.get('strike_price')

        if segment == 'EQ':
            return f"{base_symbol}-EQ" if base_symbol else "UNKNOWN-EQ"

        expiry_date = None
        if expiry:
            try:
                expiry_date = date.fromisoformat(expiry)
            except ValueError:
                expiry_date = None

        if expiry_date is None:
            return base_symbol or 'UNKNOWN'

        ddmmmyy = expiry_date.strftime('%d%b%y').upper()

        if option_type in ('CE', 'PE'):
            strike = self._format_historical_strike(strike_price)
            return f"{base_symbol}{ddmmmyy}{strike}{option_type}"

        return f"{base_symbol}{ddmmmyy}FUT"

    @staticmethod
    def _format_historical_strike(value) -> str:
        if value in (None, '', '0', '0.0'):
            return '0'
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return str(value)
        return str(int(numeric)) if numeric.is_integer() else str(numeric)

    def get_instrument_details(
        self,
        trading_symbol: str,
        exchange: str = 'MCX',
        instrument_token: Optional[str] = None
    ) -> dict:
        """Fetch instrument details from Upstox instruments master.

        Falls back gracefully if instruments master is unavailable.
        """
        exchange_for_master = exchange
        if exchange in ('NFO', 'NSE_FO'):
            exchange_for_master = 'NSE'
        elif exchange in ('BFO', 'BSE_FO'):
            exchange_for_master = 'BSE'

        cache_key = f"{exchange_for_master}_{trading_symbol}"
        if cache_key in self._instruments_cache:
            return self._instruments_cache[cache_key]

        # Parse trading symbol first (always works)
        parsed = self.parse_trading_symbol(trading_symbol)
        base_symbol = parsed.base_symbol

        try:
            # Download and decompress instruments file
            url = f"{self.INSTRUMENTS_URL}/{exchange_for_master}.json.gz"
            response = requests.get(url, timeout=10)
            response.raise_for_status()

            # Decompress
            with gzip.GzipFile(fileobj=io.BytesIO(response.content)) as f:
                instruments = json.loads(f.read().decode('utf-8'))

            # Prefer exact trading symbol match first
            for inst in instruments:
                if inst.get('trading_symbol') == trading_symbol:
                    result = {
                        'trading_symbol': inst['trading_symbol'],
                        'instrument_key': inst['instrument_key'],
                        'lot_size': inst.get('lot_size', 1),
                        'expiry': inst.get('expiry'),
                        'base_symbol': base_symbol
                    }
                    self._instruments_cache[cache_key] = result
                    return result

            # Upstox order symbols can be compact while the master uses a different
            # display symbol. In that case, the order's instrument token is exact.
            if instrument_token:
                for inst in instruments:
                    if inst.get('instrument_key') == instrument_token:
                        result = {
                            'trading_symbol': inst.get('trading_symbol', trading_symbol),
                            'instrument_key': inst['instrument_key'],
                            'lot_size': inst.get('lot_size', 1),
                            'expiry': inst.get('expiry'),
                            'base_symbol': base_symbol
                        }
                        self._instruments_cache[cache_key] = result
                        return result

            # Structured derivative match for reconstructed historical symbols.
            if parsed.instrument_type in ('FUT', 'CE', 'PE'):
                for inst in instruments:
                    inst_symbol = inst.get('trading_symbol')
                    if not inst_symbol:
                        continue
                    inst_parsed = self.parse_trading_symbol(inst_symbol)
                    if (
                        inst_parsed.base_symbol == parsed.base_symbol and
                        inst_parsed.instrument_type == parsed.instrument_type and
                        inst_parsed.expiry_date == parsed.expiry_date and
                        inst_parsed.strike == parsed.strike
                    ):
                        result = {
                            'trading_symbol': inst_symbol,
                            'instrument_key': inst.get('instrument_key'),
                            'lot_size': inst.get('lot_size', 1),
                            'expiry': inst.get('expiry'),
                            'base_symbol': base_symbol
                        }
                        self._instruments_cache[cache_key] = result
                        return result

            # Fallback to looser match if exact symbol isn't present.
            # Do not return a loose instrument_key for derivatives; it can belong
            # to a different strike and would corrupt brokerage calculations.
            for inst in instruments:
                if base_symbol in inst.get('trading_symbol', ''):
                    if inst.get('instrument_type') in ('FUT', 'CE', 'PE'):
                        result = {
                            'trading_symbol': inst['trading_symbol'],
                            'instrument_key': None if parsed.instrument_type in ('FUT', 'CE', 'PE') else inst['instrument_key'],
                            'lot_size': inst.get('lot_size', 1),
                            'expiry': inst.get('expiry'),
                            'base_symbol': base_symbol
                        }
                        self._instruments_cache[cache_key] = result
                        return result

        except requests.exceptions.RequestException as e:
            logger.warning(f"Could not fetch instruments master for {exchange_for_master}: {e}")
            # Fall through to return basic info

        # Return basic info parsed from symbol
        return {'base_symbol': base_symbol, 'lot_size': 1, 'expiry': None, 'instrument_key': None}

    def _extract_base_symbol(self, trading_symbol: str) -> str:
        """Extract base symbol from full trading symbol.

        Examples:
            ZINCMINI30APR26FUT -> ZINCMINI
            NATGASMINI25APRFUT -> NATGASMINI
            CRUDEOIL21APR25FUT -> CRUDEOIL
            CRUDEOILM16APR268700PE -> CRUDEOILM
        """
        parsed = self.parse_trading_symbol(trading_symbol)
        return parsed.base_symbol

    def parse_trading_symbol(self, trading_symbol: str) -> ParsedInstrument:
        """Parse trading symbol to extract all instrument details.

        Handles both futures and options across MCX and NSE:
            MCX Futures: SILVERMIC30APR26FUT -> base=SILVERMIC, expiry=2026-04-30, type=FUT
            MCX Options: CRUDEOILM16APR268700PE -> base=CRUDEOILM, expiry=2026-04-16, strike=8700, type=PE
            NSE Options: NIFTY26APR26000CE -> base=NIFTY, expiry=2026-04-??, strike=26000, type=CE
            NSE Options: CGPOWER26APR750CE -> base=CGPOWER, expiry=2026-04-??, strike=750, type=CE
        """
        # Check MCX commodity symbols first (longer symbols first to avoid partial matches)
        sorted_bases = sorted(CONTRACT_SPECS.keys(), key=len, reverse=True)
        base_symbol = None

        for base in sorted_bases:
            if trading_symbol.startswith(base):
                base_symbol = base
                break

        # If not a known commodity, try to extract base for equity/index options
        if not base_symbol:
            # NSE options format: {SYMBOL}{YYMMMDDSTRIKE}{CE|PE} or {SYMBOL}{YYMMMSTRIKE}{CE|PE}
            # Try to find where the date part starts (digits followed by month)
            match = re.match(r'^([A-Z]+)(\d{2}[A-Z]{3}.+)$', trading_symbol)
            if match:
                base_symbol = match.group(1)
            else:
                # Fallback: find where digits start
                for i, char in enumerate(trading_symbol):
                    if char.isdigit():
                        base_symbol = trading_symbol[:i]
                        break

        if not base_symbol:
            base_symbol = trading_symbol

        # Get the remainder after base symbol
        remainder = trading_symbol[len(base_symbol):]

        # Detect instrument type by suffix
        if remainder.endswith('FUT'):
            instrument_type = 'FUT'
            date_part = remainder[:-3]  # Remove 'FUT'
            strike = None
        elif remainder.endswith('CE') or remainder.endswith('PE'):
            instrument_type = remainder[-2:]  # CE or PE
            strike = None
            date_part = None

            # Day-first derivative format: DDMMMYY{STRIKE}CE/PE
            # Used by historical reconstruction and some instruments with an
            # explicit day component.
            ddmmmyy_match = re.match(r'^(\d{1,2})([A-Z]{3})(\d{2})(\d+)(CE|PE)$', remainder)
            ddmmmyy_candidate = None
            if ddmmmyy_match:
                ddmmmyy_candidate = {
                    'date_part': ddmmmyy_match.group(1) + ddmmmyy_match.group(2) + ddmmmyy_match.group(3),
                    'strike_text': ddmmmyy_match.group(4),
                    'strike': float(ddmmmyy_match.group(4)),
                    'year_short': int(ddmmmyy_match.group(3)),
                }

            # Determine if this is a MCX commodity option or NSE option
            # MCX commodities are in CONTRACT_SPECS
            is_commodity = base_symbol in CONTRACT_SPECS

            if is_commodity:
                # Current-day MCX monthly option symbols are usually YYMMMSTRIKECE/PE,
                # e.g. SILVERM26MAY200000PE, while historical reconstruction uses
                # DDMMMYYSTRIKECE/PE. Try the monthly compact form first and only
                # fall back to the day-first form when it is more plausible.
                mcx_monthly_match = re.match(r'^(\d{2})([A-Z]{3})(\d+)(CE|PE)$', remainder)
                if mcx_monthly_match:
                    monthly_year = int(mcx_monthly_match.group(1))
                    monthly_month = mcx_monthly_match.group(2)
                    monthly_strike_text = mcx_monthly_match.group(3)

                    if ddmmmyy_candidate and self._prefer_day_first_option_format(
                        day_first_year_short=ddmmmyy_candidate['year_short'],
                        day_first_strike_text=ddmmmyy_candidate['strike_text'],
                        monthly_year_short=monthly_year,
                        monthly_strike_text=monthly_strike_text,
                    ):
                        date_part = ddmmmyy_candidate['date_part']
                        strike = ddmmmyy_candidate['strike']
                    else:
                        strike = float(monthly_strike_text)
                        date_part = '28' + monthly_month + f"{monthly_year:02d}"
                elif ddmmmyy_candidate:
                    date_part = ddmmmyy_candidate['date_part']
                    strike = ddmmmyy_candidate['strike']

            # If not commodity or MCX pattern didn't match, try NSE options format
            if strike is None:
                # Check for SENSEX/BANKEX numeric date format first: YYMDD...STRIKE or YYMMDD...STRIKE
                # Example: SENSEX2641678500CE = SENSEX + 26 (YY) + 4 (M) + 16 (DD) + 78500 (STRIKE) + CE
                numeric_date_match = re.match(r'^(\d{2})(\d+)(CE|PE)$', remainder)
                if numeric_date_match and base_symbol in INDEX_SYMBOLS:
                    yy = numeric_date_match.group(1)
                    date_and_strike = numeric_date_match.group(2)

                    # Try 1-digit month first (MDD format)
                    if len(date_and_strike) >= 3:
                        m = date_and_strike[0]
                        dd = date_and_strike[1:3]
                        potential_strike = date_and_strike[3:]

                        try:
                            month_num = int(m)
                            day_num = int(dd)
                            if 1 <= month_num <= 9 and 1 <= day_num <= 31 and potential_strike:
                                strike = float(potential_strike)
                                date_part = dd + ['', 'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP'][month_num] + yy
                        except (ValueError, IndexError):
                            pass

                    # Try 2-digit month if 1-digit didn't work (MMDD format)
                    if strike is None and len(date_and_strike) >= 4:
                        mm = date_and_strike[0:2]
                        dd = date_and_strike[2:4]
                        potential_strike = date_and_strike[4:]

                        try:
                            month_num = int(mm)
                            day_num = int(dd)
                            if 1 <= month_num <= 12 and 1 <= day_num <= 31 and potential_strike:
                                strike = float(potential_strike)
                                month_names = ['', 'JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']
                                date_part = dd + month_names[month_num] + yy
                        except (ValueError, IndexError):
                            pass

                # Standard NSE options format: YYMMMSTRIKE or YYMMMDDSTRIKE
                if strike is None:
                    # Monthly: 26APR26000CE -> YY=26, MMM=APR, STRIKE=26000
                    # Weekly:  26APR1726000CE -> YY=26, MMM=APR, DD=17, STRIKE=26000
                    yymm_match = re.match(r'^(\d{2})([A-Z]{3})(.+)(CE|PE)$', remainder)
                    if yymm_match:
                        year = yymm_match.group(1)
                        month = yymm_match.group(2)
                        rest = yymm_match.group(3)  # Could be STRIKE or DDSTRIKE

                        # Index weekly expiries include an explicit day in the compact symbol:
                        # NIFTY26APR2824100CE -> YY=26, MMM=APR, DD=28, STRIKE=24100
                        day_match = re.match(r'^(\d{2})(\d+)$', rest)
                        if base_symbol in INDEX_SYMBOLS and day_match and 1 <= int(day_match.group(1)) <= 31 and len(day_match.group(2)) >= 3:
                            if ddmmmyy_candidate and self._prefer_day_first_option_format(
                                day_first_year_short=ddmmmyy_candidate['year_short'],
                                day_first_strike_text=ddmmmyy_candidate['strike_text'],
                                monthly_year_short=int(year),
                                monthly_strike_text=day_match.group(2),
                            ):
                                strike = ddmmmyy_candidate['strike']
                                date_part = ddmmmyy_candidate['date_part']
                            else:
                                strike = float(day_match.group(2))
                                date_part = day_match.group(1) + month + year
                        else:
                            # Monthly expiry - all of rest is strike unless the
                            # explicit day-first form is clearly more plausible.
                            if ddmmmyy_candidate and re.fullmatch(r'\d+', rest) and self._prefer_day_first_option_format(
                                day_first_year_short=ddmmmyy_candidate['year_short'],
                                day_first_strike_text=ddmmmyy_candidate['strike_text'],
                                monthly_year_short=int(year),
                                monthly_strike_text=rest,
                            ):
                                strike = ddmmmyy_candidate['strike']
                                date_part = ddmmmyy_candidate['date_part']
                            else:
                                strike = float(rest)
                                date_part = '28' + month + year  # Approximate monthly expiry

            # Fallback if nothing matched
            if strike is None:
                strike = None
                date_part = remainder[:-2]
        else:
            # Check for equity suffix (-EQ)
            if '-EQ' in trading_symbol or trading_symbol.endswith('EQ'):
                base_symbol = trading_symbol.replace('-EQ', '').replace('EQ', '')
            # Unknown type, treat as equity or other
            return ParsedInstrument(
                base_symbol=base_symbol,
                expiry_date=None,
                instrument_type='EQ',
                strike=None
            )

        # Parse expiry date from date_part
        expiry_date = self._parse_expiry_date(date_part)

        return ParsedInstrument(
            base_symbol=base_symbol,
            expiry_date=expiry_date,
            instrument_type=instrument_type,
            strike=strike
        )

    @staticmethod
    def _prefer_day_first_option_format(
        day_first_year_short: int,
        day_first_strike_text: str,
        monthly_year_short: int,
        monthly_strike_text: str,
    ) -> bool:
        """Decide whether DDMMMYYSTRIKE is more plausible than YYMMMSTRIKE.

        Current-day Upstox symbols for monthly options commonly use YYMMMSTRIKE,
        while our historical reconstruction uses DDMMMYYSTRIKE. When both regexes
        fit the same text, prefer the interpretation that yields a more plausible
        strike and year.
        """
        current_year_short = datetime.now().year % 100
        day_first_strike = float(day_first_strike_text or 0)

        if day_first_strike <= 9:
            return False
        if day_first_strike_text.startswith('0') and not monthly_strike_text.startswith('0'):
            return False

        # If the day-first "year" is far from the current contract year while
        # the monthly year looks current, prefer the monthly interpretation.
        if abs(day_first_year_short - current_year_short) > 1 and abs(monthly_year_short - current_year_short) <= 1:
            return False

        return True

    def _parse_expiry_date(self, date_str: str) -> Optional[date]:
        """Parse expiry date from string like '30APR26' or '16APR26'.

        Args:
            date_str: Date string in DDMMMYY format (e.g., "30APR26")

        Returns:
            date object or None if parsing fails
        """
        if not date_str:
            return None

        # Common patterns: 30APR26, 16APR26, 25APR26
        # Month abbreviations
        months = {
            'JAN': 1, 'FEB': 2, 'MAR': 3, 'APR': 4,
            'MAY': 5, 'JUN': 6, 'JUL': 7, 'AUG': 8,
            'SEP': 9, 'OCT': 10, 'NOV': 11, 'DEC': 12
        }

        # Try to parse DDMMMYY format
        match = re.match(r'^(\d{1,2})([A-Z]{3})(\d{2})$', date_str.upper())
        if match:
            day = int(match.group(1))
            month_str = match.group(2)
            year_short = int(match.group(3))

            month = months.get(month_str)
            if month:
                # Assume 2000s for 2-digit year
                year = 2000 + year_short
                try:
                    return date(year, month, day)
                except ValueError:
                    logger.warning(f"Invalid date components: {day}/{month}/{year}")
                    return None

        logger.warning(f"Could not parse expiry date: {date_str}")
        return None

    def calculate_brokerage(
        self,
        instrument_token: str,
        quantity: int,
        transaction_type: str,
        price: float
    ) -> float:
        """Calculate brokerage and fees for a trade leg."""
        url = f"{self.BASE_URL}/charges/brokerage"
        params = {
            'instrument_token': instrument_token,
            'quantity': quantity,
            'product': 'I',  # Intraday
            'transaction_type': transaction_type,
            'price': price
        }

        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()

        data = response.json()
        if data.get('status') != 'success':
            logger.warning(f"Brokerage API error: {data}")
            return 0.0

        return data.get('data', {}).get('charges', {}).get('total', 0.0)


class NotionClient:
    """Client for Notion API interactions."""

    BASE_URL = "https://api.notion.com/v1"

    def __init__(self, api_key: str, database_id: str):
        self.api_key = api_key
        self.database_id = database_id
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json"
        }
        self._journal_key_checked = False
        self._journal_key_enabled = False

    def _get_database(self) -> dict:
        url = f"{self.BASE_URL}/databases/{self.database_id}"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def ensure_journal_key_property(self) -> bool:
        """Ensure the database has a rich-text property for stable dedupe keys."""
        if self._journal_key_checked:
            return self._journal_key_enabled

        database = self._get_database()
        properties = database.get("properties", {})
        prop = properties.get(JOURNAL_KEY_PROPERTY)
        if prop and prop.get("type") == "rich_text":
            self._journal_key_checked = True
            self._journal_key_enabled = True
            return True

        if prop:
            logger.warning(
                "Notion property '%s' exists but is type=%s, not rich_text. "
                "Stable journal-key writes will be disabled until the schema is corrected.",
                JOURNAL_KEY_PROPERTY,
                prop.get("type"),
            )
            self._journal_key_checked = True
            self._journal_key_enabled = False
            return False

        url = f"{self.BASE_URL}/databases/{self.database_id}"
        payload = {
            "properties": {
                JOURNAL_KEY_PROPERTY: {"rich_text": {}}
            }
        }
        response = requests.patch(url, headers=self.headers, json=payload)
        response.raise_for_status()
        logger.info("Created Notion property '%s' for stable journal dedupe.", JOURNAL_KEY_PROPERTY)
        self._journal_key_checked = True
        self._journal_key_enabled = True
        return True

    @staticmethod
    def _plain_text(text: Optional[str]) -> list[dict]:
        return [{"text": {"content": text or ""}}]

    @staticmethod
    def _desired_scalar(property_payload: dict):
        """Extract a comparable scalar from a Notion property update payload."""
        if "title" in property_payload:
            return "".join(part.get("text", {}).get("content", "") for part in property_payload["title"])
        if "rich_text" in property_payload:
            return "".join(part.get("text", {}).get("content", "") for part in property_payload["rich_text"])
        if "select" in property_payload:
            select_value = property_payload["select"]
            return select_value.get("name") if select_value else None
        if "date" in property_payload:
            date_value = property_payload["date"]
            return date_value.get("start") if date_value else None
        if "number" in property_payload:
            return property_payload["number"]
        if "checkbox" in property_payload:
            return property_payload["checkbox"]
        return property_payload

    @staticmethod
    def _existing_scalar(property_payload: dict):
        """Extract a comparable scalar from a Notion page property response."""
        prop_type = property_payload.get("type")
        if prop_type == "title":
            return "".join(part.get("plain_text", "") for part in property_payload.get("title", []))
        if prop_type == "rich_text":
            return "".join(part.get("plain_text", "") for part in property_payload.get("rich_text", []))
        if prop_type == "select":
            select_value = property_payload.get("select")
            return select_value.get("name") if select_value else None
        if prop_type == "date":
            date_value = property_payload.get("date")
            return date_value.get("start") if date_value else None
        if prop_type == "number":
            return property_payload.get("number")
        if prop_type == "checkbox":
            return property_payload.get("checkbox")
        return None

    @staticmethod
    def _values_equal(existing_value, desired_value) -> bool:
        if isinstance(existing_value, (int, float)) or isinstance(desired_value, (int, float)):
            if existing_value is None or desired_value is None:
                return existing_value == desired_value
            return abs(float(existing_value) - float(desired_value)) < 0.005
        return existing_value == desired_value

    def _build_trade_properties(self, trade: TradeEntry, label: str) -> dict:
        """Build the Notion property payload for a trade row."""
        properties = {
            "Trade Label": {
                "title": self._plain_text(label)
            },
            "Symbol": {
                "rich_text": self._plain_text(trade.symbol)
            },
            "Direction": {"select": {"name": trade.direction}},
            "Entry Date": {"date": {"start": trade.entry_date.isoformat()}},
            "Entry Time": {"rich_text": self._plain_text(trade.entry_time)},
            "Entry Price": {"number": trade.entry_price},
            "Quantity": {"number": trade.quantity},
            "Instrument Type": {"select": {"name": trade.instrument_type}},
            "Lot Size": {"number": trade.lot_size},
            "Fees": {"number": round(trade.fees, 2)},
            "Outcome": {"select": {"name": trade.outcome}},
            "Timeframe": {"select": {"name": trade.timeframe}},
            "Status": {"select": {"name": trade.status}},
            "P&L": {"number": round(trade.pnl, 2)},
        }

        if self.ensure_journal_key_property() and trade.journal_key:
            properties[JOURNAL_KEY_PROPERTY] = {"rich_text": self._plain_text(trade.journal_key)}

        if trade.exit_date is not None:
            properties["Exit Date"] = {"date": {"start": trade.exit_date.isoformat()}}
        if trade.exit_time is not None:
            properties["Exit Time"] = {"rich_text": self._plain_text(trade.exit_time)}
        if trade.exit_price is not None:
            properties["Exit Price"] = {"number": trade.exit_price}

        if trade.stop_loss is not None:
            properties["Stop Loss"] = {"number": trade.stop_loss}

        if trade.target is not None:
            properties["Target"] = {"number": trade.target}

        if trade.expiry_date is not None:
            properties["Expiry Date"] = {"date": {"start": trade.expiry_date.isoformat()}}

        if trade.option_type is not None:
            properties["Option Type"] = {"select": {"name": trade.option_type}}

        if trade.option_strike is not None:
            properties["Option Strike"] = {"number": trade.option_strike}

        if trade.strategy is not None:
            properties["Strategy"] = {"select": {"name": trade.strategy}}

        if trade.pre_trade_notes is not None:
            properties["Pre-trade Notes"] = {"rich_text": self._plain_text(trade.pre_trade_notes)}

        if trade.post_trade_review is not None:
            properties["Post-trade Review"] = {"rich_text": self._plain_text(trade.post_trade_review)}

        if trade.followed_plan is not None:
            properties["Followed Plan"] = {"checkbox": trade.followed_plan}

        return properties

    def _diff_properties(self, existing_properties: dict, desired_properties: dict) -> dict:
        """Return only desired properties whose current Notion value differs."""
        modified = {}

        for name, desired_payload in desired_properties.items():
            existing_payload = existing_properties.get(name, {})
            existing_value = self._existing_scalar(existing_payload)
            desired_value = self._desired_scalar(desired_payload)

            if name in ("Pre-trade Notes", "Post-trade Review") and existing_value:
                # Preserve manually curated notes. Auto-generated journal notes
                # should fill blanks, not overwrite richer trade context.
                continue

            if not self._values_equal(existing_value, desired_value):
                modified[name] = desired_payload

        return modified

    def get_page(self, page_id: str) -> dict:
        """Fetch a Notion page by ID."""
        url = f"{self.BASE_URL}/pages/{page_id}"
        response = requests.get(url, headers=self.headers)
        response.raise_for_status()
        return response.json()

    def _query_database(self, payload: dict) -> list[dict]:
        url = f"{self.BASE_URL}/databases/{self.database_id}/query"
        response = requests.post(url, headers=self.headers, json=payload)
        response.raise_for_status()
        return response.json().get("results", [])

    def find_trade_entry(self, label: str, entry_date: date, journal_key: Optional[str] = None) -> Optional[dict]:
        """Find an existing trade row by stable journal key, then fallback label/date."""
        if journal_key and self.ensure_journal_key_property():
            try:
                results = self._query_database({
                    "filter": {
                        "property": JOURNAL_KEY_PROPERTY,
                        "rich_text": {"equals": journal_key},
                    },
                    "page_size": 10,
                })
            except requests.HTTPError as exc:
                logger.warning("Journal-key query failed; falling back to label lookup: %s", exc)
                self._journal_key_enabled = False
                self._journal_key_checked = True
            else:
                if len(results) > 1:
                    logger.warning(
                        "Found %s duplicate Notion rows for journal key %s. Updating the first match only.",
                        len(results),
                        journal_key,
                    )
                if results:
                    return results[0]

        url = f"{self.BASE_URL}/databases/{self.database_id}/query"
        payload = {
            "filter": {
                "and": [
                    {"property": "Trade Label", "title": {"equals": label}},
                    {"property": "Entry Date", "date": {"equals": entry_date.isoformat()}},
                ]
            },
            "page_size": 10,
        }

        response = requests.post(url, headers=self.headers, json=payload)
        response.raise_for_status()
        results = response.json().get("results", [])

        if len(results) > 1:
            logger.warning(
                f"Found {len(results)} duplicate Notion rows for {label} on {entry_date}. "
                "Updating the first match only."
            )

        return results[0] if results else None

    def preview_trade_entry_action(self, trade: TradeEntry, label: str) -> tuple[str, list[str]]:
        """Return create/update/skip and changed fields without writing to Notion."""
        desired_properties = self._build_trade_properties(trade, label)
        existing_page = self.find_trade_entry(label, trade.entry_date, trade.journal_key)

        if not existing_page:
            return "create", list(desired_properties.keys())

        modified = self._diff_properties(existing_page.get("properties", {}), desired_properties)
        if not modified:
            return "skip", []
        return "update", list(modified.keys())

    def _patch_modified_properties(
        self,
        page_id: str,
        desired_properties: dict,
        existing_page: Optional[dict] = None,
    ) -> dict:
        """Patch only changed properties; skip the API write if nothing changed."""
        if existing_page is None:
            existing_page = self.get_page(page_id)

        modified = self._diff_properties(existing_page.get("properties", {}), desired_properties)
        if not modified:
            logger.info(f"  No modified fields for {page_id}; skipping Notion update")
            return {
                "id": page_id,
                "status": "skipped",
                "updated_fields": [],
            }

        url = f"{self.BASE_URL}/pages/{page_id}"
        payload = {"properties": modified}

        response = requests.patch(url, headers=self.headers, json=payload)
        response.raise_for_status()
        result = response.json()
        result["status"] = "updated"
        result["updated_fields"] = list(modified.keys())
        return result

    def get_open_positions(self) -> list[OpenPosition]:
        """Fetch all open and partially filled positions from Notion."""
        url = f"{self.BASE_URL}/databases/{self.database_id}/query"

        payload = {
            "filter": {
                "or": [
                    {"property": "Status", "select": {"equals": "Open"}},
                    {"property": "Status", "select": {"equals": "Partially Filled"}}
                ]
            }
        }

        response = requests.post(url, headers=self.headers, json=payload)
        response.raise_for_status()

        data = response.json()
        positions = []

        for result in data.get('results', []):
            props = result['properties']

            # Parse entry date
            entry_date_str = props.get('Entry Date', {}).get('date', {}).get('start')
            entry_date = datetime.fromisoformat(entry_date_str).date() if entry_date_str else None

            # Parse entry time
            entry_time_text = props.get('Entry Time', {}).get('rich_text', [])
            entry_time = entry_time_text[0]['plain_text'] if entry_time_text else "N/A"

            # Parse symbol
            symbol_text = props.get('Symbol', {}).get('rich_text', [])
            symbol = symbol_text[0]['plain_text'] if symbol_text else ""

            # Parse direction
            direction = props.get('Direction', {}).get('select', {}).get('name', '')

            # Parse prices and quantities
            entry_price = props.get('Entry Price', {}).get('number', 0.0)
            quantity = props.get('Quantity', {}).get('number', 0)
            lot_size = props.get('Lot Size', {}).get('number', 1)
            fees = props.get('Fees', {}).get('number', 0.0)
            stop_loss = props.get('Stop Loss', {}).get('number')

            # Parse instrument type
            instrument_type = props.get('Instrument Type', {}).get('select', {}).get('name', '')

            # Parse expiry date
            expiry_date_str = props.get('Expiry Date', {}).get('date', {}).get('start')
            expiry_date = datetime.fromisoformat(expiry_date_str).date() if expiry_date_str else None

            # Parse option fields
            option_type = props.get('Option Type', {}).get('select', {}).get('name')
            option_strike = props.get('Option Strike', {}).get('number')

            # Parse status
            status = props.get('Status', {}).get('select', {}).get('name', 'Open')

            # Parse label so account-prefixed rows can be handled safely.
            label_parts = props.get('Trade Label', {}).get('title', [])
            label = label_parts[0]['plain_text'] if label_parts else ""
            journal_key_parts = props.get(JOURNAL_KEY_PROPERTY, {}).get('rich_text', [])
            journal_key = journal_key_parts[0]['plain_text'] if journal_key_parts else None

            if symbol and direction and entry_date:
                positions.append(OpenPosition(
                    page_id=result['id'],
                    symbol=symbol,
                    direction=direction,
                    entry_date=entry_date,
                    entry_time=entry_time,
                    entry_price=entry_price,
                    quantity=int(quantity),
                    instrument_type=instrument_type,
                    lot_size=int(lot_size),
                    fees=fees,
                    stop_loss=stop_loss,
                    expiry_date=expiry_date,
                    option_type=option_type,
                    option_strike=option_strike,
                    status=status,
                    label=label,
                    journal_key=journal_key,
                ))

        return positions

    def update_trade_entry(
        self,
        page_id: str,
        exit_date: date,
        exit_time: str,
        exit_price: float,
        pnl: float,
        fees: float,
        status: str,
        outcome: str,
        timeframe: str,
        quantity: Optional[int] = None,
        journal_key: Optional[str] = None,
    ) -> dict:
        """Update an existing trade entry with exit information."""
        properties = {
            "Exit Date": {"date": {"start": exit_date.isoformat()}},
            "Exit Time": {"rich_text": self._plain_text(exit_time)},
            "Exit Price": {"number": round(exit_price, 2)},
            "P&L": {"number": round(pnl, 2)},
            "Fees": {"number": round(fees, 2)},
            "Status": {"select": {"name": status}},
            "Outcome": {"select": {"name": outcome}},
            "Timeframe": {"select": {"name": timeframe}},
            "Target": {"number": round(exit_price, 2)}
        }

        # Update quantity if provided (for partial closes)
        if quantity is not None:
            properties["Quantity"] = {"number": quantity}

        if self.ensure_journal_key_property() and journal_key:
            properties[JOURNAL_KEY_PROPERTY] = {"rich_text": self._plain_text(journal_key)}

        return self._patch_modified_properties(page_id, properties)

    def create_trade_entry(self, trade: TradeEntry, label: str) -> dict:
        """Create a new trade entry in the Trading Journal."""
        url = f"{self.BASE_URL}/pages"
        properties = self._build_trade_properties(trade, label)

        payload = {
            "parent": {"database_id": self.database_id},
            "properties": properties
        }

        response = requests.post(url, headers=self.headers, json=payload)
        response.raise_for_status()

        return response.json()

    def upsert_trade_entry(self, trade: TradeEntry, label: str) -> dict:
        """Create a trade row, or update only modified fields if it already exists."""
        existing_page = self.find_trade_entry(label, trade.entry_date, trade.journal_key)
        if not existing_page:
            result = self.create_trade_entry(trade, label)
            result["status"] = "created"
            result["updated_fields"] = []
            return result

        desired_properties = self._build_trade_properties(trade, label)
        result = self._patch_modified_properties(
            existing_page["id"],
            desired_properties,
            existing_page=existing_page,
        )
        return result


class TradeProcessor:
    """Processes orders into journal entries."""

    def __init__(self, upstox: UpstoxClient, notion: NotionClient, account: str = 'BALA', dry_run: bool = False):
        self.upstox = upstox
        self.notion = notion
        self.account = account
        self.dry_run = dry_run
        self.open_positions: list[OpenPosition] = []
        self.updated_existing_positions: list[str] = []
        self.last_run_summary: dict = {}

    def load_open_positions(self):
        """Load open and partially filled positions from Notion."""
        logger.info("Loading open positions from Notion...")
        self.open_positions = self.notion.get_open_positions()
        logger.info(f"Found {len(self.open_positions)} open/partially filled positions")
        for pos in self.open_positions:
            option_info = ""
            if pos.option_strike or pos.option_type:
                option_info = f" Strike={pos.option_strike} Type={pos.option_type}"
            expiry_info = f" Exp={pos.expiry_date}" if pos.expiry_date else ""
            logger.info(f"  {pos.symbol} {pos.direction} Qty={pos.quantity}{option_info}{expiry_info} Status={pos.status}")

    def find_matching_open_positions(
        self,
        symbol: str,
        direction: str,
        expiry_date: Optional[date],
        option_strike: Optional[float]
    ) -> list[OpenPosition]:
        """Find all matching open positions with the specified direction (FIFO order).

        Matching criteria:
        - Same symbol
        - Same direction (Long or Short)
        - Same expiry (for options/futures)
        - Same strike (for options)
        - Same account

        Returns:
            List of matching positions sorted by entry date (oldest first for FIFO)
        """
        matches = []
        for pos in self.open_positions:
            # Debug: log why positions don't match
            if pos.symbol == symbol and pos.direction == direction:
                if not self._expiry_dates_match(pos.expiry_date, expiry_date, symbol):
                    logger.info(f"    Position {pos.symbol} {pos.direction} skipped: expiry mismatch "
                                f"(pos={pos.expiry_date} vs order={expiry_date})")
                elif pos.option_strike != option_strike:
                    logger.info(f"    Position {pos.symbol} {pos.direction} skipped: strike mismatch "
                                f"(pos={pos.option_strike} vs order={option_strike})")
                elif not self._position_belongs_to_account(pos):
                    logger.info(f"    Position {pos.symbol} {pos.direction} skipped: account mismatch")

            if (pos.symbol == symbol and
                pos.direction == direction and
                self._expiry_dates_match(pos.expiry_date, expiry_date, symbol) and
                pos.option_strike == option_strike and
                self._position_belongs_to_account(pos)):
                matches.append(pos)

        # Sort by entry date for FIFO (oldest first)
        matches.sort(key=lambda p: (p.entry_date, p.entry_time))
        return matches

    def find_matching_open_position(
        self,
        symbol: str,
        direction: str,
        expiry_date: Optional[date],
        option_strike: Optional[float]
    ) -> Optional[OpenPosition]:
        """Find a single matching open position (for backward compatibility).

        Returns the oldest matching position (FIFO).
        """
        matches = self.find_matching_open_positions(symbol, direction, expiry_date, option_strike)
        return matches[0] if matches else None

    def _is_index_symbol(self, symbol: str) -> bool:
        return symbol in INDEX_SYMBOLS

    def _normalized_expiry_date(self, parsed: ParsedInstrument, inst_details: dict) -> Optional[date]:
        """Prefer an exact expiry from instruments master when available."""
        expiry_value = inst_details.get('expiry')
        if isinstance(expiry_value, date):
            return expiry_value
        if expiry_value and parsed.instrument_type in ('FUT', 'CE', 'PE'):
            try:
                return date.fromisoformat(expiry_value)
            except (TypeError, ValueError):
                pass
        return parsed.expiry_date

    def _expiry_dates_match(self, left: Optional[date], right: Optional[date], symbol: str) -> bool:
        if left == right:
            return True
        if not left or not right:
            return False

        # Backward-compatibility for older journal rows where monthly stock-option
        # expiries were approximated from compact symbols instead of resolved from
        # instruments master.
        if (
            symbol not in INDEX_SYMBOLS and
            left.year == right.year and
            left.month == right.month and
            abs(left.day - right.day) <= 3 and
            max(left.day, right.day) >= 25
        ):
            return True
        return False

    @staticmethod
    def _order_time_text(order: Order) -> str:
        return order.time_text or order.order_timestamp.strftime('%H:%M')

    def _time_text_from_orders(self, orders: list[Order], prefer: str = 'min') -> str:
        if not orders:
            return 'N/A'
        selected = min(orders, key=lambda item: item.order_timestamp) if prefer == 'min' else max(orders, key=lambda item: item.order_timestamp)
        if any(order.time_text for order in orders):
            return self._order_time_text(selected)
        return 'N/A'

    def _resolve_fee_instrument_token(
        self,
        exchange: str,
        parsed: ParsedInstrument,
        inst_details: dict,
        order_instrument_token: Optional[str]
    ) -> Optional[str]:
        """Choose a safe instrument token for brokerage calculation.

        For F&O contracts, only use the token resolved from instruments master.
        Historical trade rows can carry missing or non-derivative tokens that
        are invalid for the brokerage API.
        """
        master_token = inst_details.get('instrument_key')
        if exchange in ('NSE', 'NFO', 'NSE_FO', 'BSE', 'BFO', 'BSE_FO') and parsed.instrument_type in ('FUT', 'CE', 'PE'):
            return master_token
        return master_token or order_instrument_token

    def _display_lot_size(self, exchange: str, parsed: ParsedInstrument, inst_details: dict) -> int:
        if exchange == 'MCX':
            return CONTRACT_SPECS.get(parsed.base_symbol, {}).get('multiplier', 1)
        if exchange in ('NSE', 'NFO', 'NSE_FO', 'BSE', 'BFO', 'BSE_FO') and parsed.instrument_type in ('FUT', 'CE', 'PE'):
            return int(inst_details.get('lot_size') or 1)
        return 1

    def _display_quantity(self, raw_quantity: int, exchange: str, parsed: ParsedInstrument, inst_details: dict) -> int:
        lot_size = self._display_lot_size(exchange, parsed, inst_details)
        if exchange == 'MCX':
            return raw_quantity
        if exchange in ('NSE', 'NFO', 'NSE_FO', 'BSE', 'BFO', 'BSE_FO') and parsed.instrument_type in ('FUT', 'CE', 'PE') and lot_size > 0:
            return max(1, int(raw_quantity / lot_size))
        return raw_quantity

    def _process_closing_trade(
        self,
        trading_symbol: str,
        orders: list[Order],
        open_position: OpenPosition,
        stop_loss: Optional[float] = None
    ) -> list[TradeEntry]:
        """Process orders that close an existing open position.

        Handles:
        - Full closes (qty matches exactly)
        - Partial closes (qty less than open qty)
        - Over-closes (qty greater than open qty - creates new opposite position)
        """
        parsed = self.upstox.parse_trading_symbol(trading_symbol)
        inst_details = self.upstox.get_instrument_details(
            trading_symbol,
            orders[0].exchange,
            orders[0].instrument_token
        )

        # Calculate total closing quantity
        total_close_qty = sum(o.quantity for o in orders)
        close_direction = 'Long' if orders[0].transaction_type == 'BUY' else 'Short'
        normalized_expiry = self._normalized_expiry_date(parsed, inst_details)

        # Get contract multiplier for P&L calculation
        exchange = orders[0].exchange
        if exchange == 'MCX':
            pnl_multiplier = CONTRACT_SPECS.get(parsed.base_symbol, {}).get('multiplier', 1)
        else:
            pnl_multiplier = 1

        # Calculate average exit price and time
        avg_exit_price = sum(o.quantity * o.average_price for o in orders) / total_close_qty
        exit_time = max(o.order_timestamp for o in orders)
        exit_time_text = self._time_text_from_orders(orders, prefer='max')

        # Get instrument token for fee calculation
        instrument_token = self._resolve_fee_instrument_token(
            exchange,
            parsed,
            inst_details,
            orders[0].instrument_token
        )

        # Calculate exit fees
        if instrument_token:
            exit_fees = sum(
                self.upstox.calculate_brokerage(
                    instrument_token, o.quantity,
                    o.transaction_type, o.average_price
                )
                for o in orders
            )
        else:
            exit_fees = 25.0 * len(orders)

        # Determine how much of the open position is being closed
        display_lot_size = self._display_lot_size(exchange, parsed, inst_details)

        # Convert quantities for comparison (use raw quantities)
        # For MCX, quantity in orders is already raw, for NSE it's also raw
        open_qty = open_position.quantity
        close_qty = self._display_quantity(total_close_qty, exchange, parsed, inst_details)

        trade_entries = []

        if close_qty >= open_qty:
            # Full close (or over-close)
            closed_qty = min(close_qty, open_qty)

            # Calculate P&L for the closed quantity
            if open_position.direction == 'Long':
                pnl = (avg_exit_price - open_position.entry_price) * pnl_multiplier * (total_close_qty if exchange == 'MCX' else closed_qty * display_lot_size)
            else:
                pnl = (open_position.entry_price - avg_exit_price) * pnl_multiplier * (total_close_qty if exchange == 'MCX' else closed_qty * display_lot_size)

            # Total fees = entry fees + exit fees
            total_fees = open_position.fees + exit_fees

            # Determine outcome
            if pnl > 0:
                outcome = 'Win'
            elif pnl < 0:
                outcome = 'Loss'
            else:
                outcome = 'Breakeven'

            # Determine timeframe
            if open_position.entry_date == exit_time.date():
                timeframe = 'Intraday'
            else:
                timeframe = 'Positional'

            closed_journal_key = build_journal_key(
                account=self.account,
                symbol=open_position.symbol,
                direction=open_position.direction,
                entry_date=open_position.entry_date,
                instrument_type=open_position.instrument_type,
                expiry_date=open_position.expiry_date,
                option_type=open_position.option_type,
                option_strike=open_position.option_strike,
                entry_source_ids=self._position_entry_source_ids(open_position),
                exit_source_ids=[self._source_id_for_order(order) for order in orders],
            )

            # Update the existing Notion entry
            logger.info(f"Closing position {open_position.page_id}: P&L={pnl:.2f}")
            self._update_existing_position(
                position=open_position,
                exit_date=exit_time.date(),
                exit_time=exit_time_text,
                exit_price=avg_exit_price,
                pnl=pnl,
                fees=total_fees,
                status='Closed',
                outcome=outcome,
                timeframe=timeframe,
                quantity=closed_qty,
                journal_key=closed_journal_key,
            )
            self.updated_existing_positions.append(open_position.page_id)

            # If over-close, create new open position for the excess
            if close_qty > open_qty:
                excess_qty = close_qty - open_qty
                excess_fees = exit_fees * (excess_qty / close_qty)

                excess_entry = TradeEntry(
                    symbol=parsed.base_symbol,
                    direction=close_direction,
                    entry_date=exit_time.date(),
                    entry_time=exit_time_text,
                    entry_price=round(avg_exit_price, 2),
                    quantity=int(excess_qty),
                    instrument_type=open_position.instrument_type,
                    lot_size=display_lot_size,
                    fees=round(excess_fees, 2),
                    stop_loss=stop_loss,
                    exit_date=None,
                    exit_time=None,
                    exit_price=None,
                    pnl=0.0,
                    outcome='Open',
                    timeframe='Positional',
                    status='Open',
                    expiry_date=normalized_expiry,
                    option_type=parsed.option_type,
                    option_strike=parsed.strike,
                    entry_source_ids=[self._source_id_for_order(order) for order in orders],
                )
                self._ensure_trade_journal_key(excess_entry)
                trade_entries.append(excess_entry)

        else:
            # Partial close
            # Calculate P&L for the closed quantity
            if open_position.direction == 'Long':
                pnl = (avg_exit_price - open_position.entry_price) * pnl_multiplier * (total_close_qty if exchange == 'MCX' else close_qty * display_lot_size)
            else:
                pnl = (open_position.entry_price - avg_exit_price) * pnl_multiplier * (total_close_qty if exchange == 'MCX' else close_qty * display_lot_size)

            # Proportional fees for closed quantity
            closed_fees_ratio = close_qty / open_qty
            closed_fees = (open_position.fees * closed_fees_ratio) + exit_fees

            # Determine outcome
            if pnl > 0:
                outcome = 'Win'
            elif pnl < 0:
                outcome = 'Loss'
            else:
                outcome = 'Breakeven'

            # Determine timeframe
            if open_position.entry_date == exit_time.date():
                timeframe = 'Intraday'
            else:
                timeframe = 'Positional'

            partial_journal_key = build_journal_key(
                account=self.account,
                symbol=open_position.symbol,
                direction=open_position.direction,
                entry_date=open_position.entry_date,
                instrument_type=open_position.instrument_type,
                expiry_date=open_position.expiry_date,
                option_type=open_position.option_type,
                option_strike=open_position.option_strike,
                entry_source_ids=self._position_entry_source_ids(open_position),
                exit_source_ids=[self._source_id_for_order(order) for order in orders],
            )

            # Update existing entry with partial close
            logger.info(f"Partially closing position {open_position.page_id}: {close_qty}/{open_qty} closed")
            self._update_existing_position(
                position=open_position,
                exit_date=exit_time.date(),
                exit_time=exit_time_text,
                exit_price=avg_exit_price,
                pnl=pnl,
                fees=closed_fees,
                status='Partially Filled',
                outcome=outcome,
                timeframe=timeframe,
                quantity=close_qty,
                journal_key=partial_journal_key,
            )
            self.updated_existing_positions.append(open_position.page_id)

            # Create new entry for remaining open quantity
            remaining_qty = open_qty - close_qty
            remaining_fees = open_position.fees * (remaining_qty / open_qty)

            remaining_entry = TradeEntry(
                symbol=parsed.base_symbol,
                direction=open_position.direction,
                entry_date=open_position.entry_date,
                entry_time=open_position.entry_time,
                entry_price=open_position.entry_price,
                quantity=int(remaining_qty),
                instrument_type=open_position.instrument_type,
                lot_size=display_lot_size,
                fees=round(remaining_fees, 2),
                stop_loss=open_position.stop_loss,
                exit_date=None,
                exit_time=None,
                exit_price=None,
                pnl=0.0,
                outcome='Open',
                timeframe='Positional',
                status='Open',
                expiry_date=open_position.expiry_date,
                option_type=open_position.option_type,
                option_strike=open_position.option_strike,
                entry_source_ids=self._position_entry_source_ids(open_position),
            )
            self._ensure_trade_journal_key(remaining_entry)
            trade_entries.append(remaining_entry)

        return trade_entries

    def process_orders(self, orders: list[Order], stop_loss: Optional[float] = None) -> list[TradeEntry]:
        """Process orders into trade entries.

        First checks if orders close existing open positions (FIFO matching).
        Remaining orders are processed as new trades.
        """
        if not orders:
            return []

        self.updated_existing_positions = []
        self.last_run_summary = {}

        # Load open positions from Notion
        self.load_open_positions()

        # Group orders by FULL trading symbol (each strike/expiry is separate)
        by_symbol = defaultdict(list)
        for order in orders:
            by_symbol[order.trading_symbol].append(order)

        trade_entries = []
        updated_positions = []  # Track positions we've updated

        for trading_symbol, symbol_orders in by_symbol.items():
            # Parse the symbol to get instrument details
            parsed = self.upstox.parse_trading_symbol(trading_symbol)
            inst_details = self.upstox.get_instrument_details(
                trading_symbol,
                symbol_orders[0].exchange,
                symbol_orders[0].instrument_token
            )
            normalized_expiry = self._normalized_expiry_date(parsed, inst_details)
            logger.info(f"Processing symbol: {trading_symbol} -> base={parsed.base_symbol}, "
                        f"expiry={normalized_expiry}, strike={parsed.strike}, type={parsed.instrument_type}")

            # Separate buy and sell orders
            buys = [o for o in symbol_orders if o.transaction_type == 'BUY']
            sells = [o for o in symbol_orders if o.transaction_type == 'SELL']
            logger.info(f"  Orders: {len(buys)} BUYs, {len(sells)} SELLs")

            # Check for open positions that could be closed by these orders
            # BUY orders close SHORT positions, SELL orders close LONG positions
            long_positions = self.find_matching_open_positions(
                parsed.base_symbol, 'Long', normalized_expiry, parsed.strike
            )
            short_positions = self.find_matching_open_positions(
                parsed.base_symbol, 'Short', normalized_expiry, parsed.strike
            )
            logger.info(f"  Matching positions: {len(long_positions)} Long, {len(short_positions)} Short")

            # Filter out already updated positions
            long_positions = [p for p in long_positions if p not in updated_positions]
            short_positions = [p for p in short_positions if p not in updated_positions]

            # Track remaining orders after closing positions
            remaining_buys = list(buys)
            remaining_sells = list(sells)

            # Process SELL orders against LONG positions (FIFO)
            if sells and long_positions:
                logger.info(f"  Matching {len(sells)} SELLs against {len(long_positions)} LONG positions")
                closing_sells, remaining_sells, closed_longs = self._match_orders_to_positions(
                    sells, long_positions, trading_symbol, parsed, stop_loss, trade_entries
                )
                updated_positions.extend(closed_longs)
                logger.info(f"  After matching: {len(remaining_sells)} SELLs remaining")

            # Process BUY orders against SHORT positions (FIFO)
            if buys and short_positions:
                logger.info(f"  Matching {len(buys)} BUYs against {len(short_positions)} SHORT positions")
                closing_buys, remaining_buys, closed_shorts = self._match_orders_to_positions(
                    buys, short_positions, trading_symbol, parsed, stop_loss, trade_entries
                )
                updated_positions.extend(closed_shorts)
                logger.info(f"  After matching: {len(remaining_buys)} BUYs remaining")

            # Process remaining orders as new trades
            remaining_orders = remaining_buys + remaining_sells
            if remaining_orders:
                logger.info(f"  Processing {len(remaining_orders)} remaining orders as new trades")
                entries = self._process_single_instrument(trading_symbol, remaining_orders, stop_loss)
                trade_entries.extend(entries)
            else:
                logger.info(f"  No remaining orders - all matched to existing positions")

        self._annotate_option_adjustments(trade_entries)
        self.last_run_summary = self._build_run_summary(trade_entries)
        return trade_entries

    def _build_run_summary(self, trade_entries: list[TradeEntry]) -> dict:
        updated_count = len(self.updated_existing_positions)
        open_entries = [trade for trade in trade_entries if trade.is_open]
        closed_entries = [trade for trade in trade_entries if not trade.is_open]

        if updated_count and open_entries:
            activity_type = 'mixed_adjustment'
            activity_label = 'Mixed adjustment day'
            activity_detail = 'Closed or updated existing positions and opened fresh legs.'
        elif updated_count and closed_entries:
            activity_type = 'mixed_rollover'
            activity_label = 'Mixed close-and-log day'
            activity_detail = 'Closed existing positions and also created fresh closed-trade journal rows.'
        elif updated_count:
            activity_type = 'closing_only'
            activity_label = 'Closing day'
            activity_detail = 'Only closed or updated existing positions from the journal.'
        elif open_entries and closed_entries:
            activity_type = 'mixed_new'
            activity_label = 'Mixed fresh-entry day'
            activity_detail = 'Created both open-position and same-day closed trade rows.'
        elif open_entries:
            activity_type = 'opening_only'
            activity_label = 'Opening day'
            activity_detail = 'Only opened fresh positions; no prior journal positions were closed.'
        elif closed_entries:
            activity_type = 'fresh_closed_only'
            activity_label = 'Standalone closed-trade day'
            activity_detail = 'Created only fresh completed trade rows.'
        else:
            activity_type = 'no_action'
            activity_label = 'No journal action'
            activity_detail = 'No matching closes or new trade rows were produced.'

        return {
            'activity_type': activity_type,
            'activity_label': activity_label,
            'activity_detail': activity_detail,
            'updated_existing_positions': updated_count,
            'new_trade_entries': len(trade_entries),
            'new_open_entries': len(open_entries),
            'new_closed_entries': len(closed_entries),
        }

    def _match_orders_to_positions(
        self,
        orders: list[Order],
        positions: list[OpenPosition],
        trading_symbol: str,
        parsed: ParsedInstrument,
        stop_loss: Optional[float],
        trade_entries: list[TradeEntry]
    ) -> tuple[list[Order], list[Order], list[OpenPosition]]:
        """Match closing orders to open positions using FIFO.

        Args:
            orders: Orders that could close positions (sorted by time)
            positions: Open positions to close (sorted by entry date - FIFO)
            trading_symbol: The trading symbol
            parsed: Parsed instrument details
            stop_loss: Optional stop loss
            trade_entries: List to append closed trade entries

        Returns:
            Tuple of (used_orders, remaining_orders, closed_positions)
        """
        # Sort orders by time for FIFO
        orders_sorted = sorted(orders, key=lambda o: o.order_timestamp)

        used_orders = []
        closed_positions = []

        # Get instrument details for fee calculation
        inst_details = self.upstox.get_instrument_details(
            trading_symbol,
            orders[0].exchange,
            orders[0].instrument_token
        )
        exchange = orders[0].exchange
        instrument_token = self._resolve_fee_instrument_token(
            exchange,
            parsed,
            inst_details,
            orders[0].instrument_token
        )

        # Get contract multiplier for P&L calculation
        if exchange == 'MCX':
            pnl_multiplier = CONTRACT_SPECS.get(parsed.base_symbol, {}).get('multiplier', 1)
        else:
            pnl_multiplier = 1

        display_lot_size = self._display_lot_size(exchange, parsed, inst_details)

        # Build order queue with quantities in DISPLAY UNITS (lots) to match position quantities
        # Position quantities in Notion are stored in display units (lots)
        # Order quantities from Upstox are in raw units (shares)
        order_queue = []
        for order in orders_sorted:
            # Convert raw quantity to display quantity (lots)
            display_qty = self._display_quantity(order.quantity, exchange, parsed, inst_details)
            order_queue.append({
                'order': order,
                'remaining_qty': display_qty,  # In display units (lots)
                'original_qty': display_qty,   # In display units (lots)
                'raw_qty': order.quantity,     # Keep raw for fee calculation
            })

        # Determine instrument type
        is_index = self._is_index_symbol(parsed.base_symbol)
        if exchange == 'MCX':
            instrument_type = 'Commodity Options' if parsed.is_option else 'Commodity Futures'
        elif exchange in ('NSE', 'NFO', 'NSE_FO', 'BSE', 'BFO', 'BSE_FO'):
            if parsed.is_option:
                instrument_type = 'Index Options' if is_index else 'Equity Options'
            elif parsed.instrument_type == 'FUT':
                instrument_type = 'Index Futures' if is_index else 'Equity Futures'
            else:
                instrument_type = 'Equity'
        else:
            instrument_type = 'Equity'

        # Process each position in FIFO order (oldest first)
        for position in positions:
            if not order_queue:
                break  # No more orders to match

            position_qty_to_close = position.quantity
            matched_orders = []  # Track which orders (and how much) matched this position

            # Match orders against this position
            while position_qty_to_close > 0 and order_queue:
                oq = order_queue[0]

                # Determine how much to match from this order
                match_qty = min(oq['remaining_qty'], position_qty_to_close)

                if match_qty > 0:
                    matched_orders.append({
                        'order': oq['order'],
                        'qty_used': match_qty,
                    })

                    oq['remaining_qty'] -= match_qty
                    position_qty_to_close -= match_qty

                # Remove fully consumed orders from queue
                if oq['remaining_qty'] <= 0:
                    used_orders.append(order_queue.pop(0)['order'])

            # Calculate closed quantity for this position
            closed_qty = position.quantity - position_qty_to_close

            if closed_qty > 0 and matched_orders:
                # Calculate weighted average exit price from matched orders
                weighted_sum = sum(m['order'].average_price * m['qty_used'] for m in matched_orders)
                avg_exit_price = weighted_sum / closed_qty

                # Get exit time from the last matched order
                exit_time = max(m['order'].order_timestamp for m in matched_orders)
                exit_time_text = self._time_text_from_orders([m['order'] for m in matched_orders], prefer='max')

                # Calculate exit fees
                if instrument_token:
                    exit_fees = self.upstox.calculate_brokerage(
                        instrument_token,
                        closed_qty * display_lot_size if exchange != 'MCX' else closed_qty,
                        'BUY' if position.direction == 'Short' else 'SELL',
                        avg_exit_price
                    )
                else:
                    exit_fees = 25.0

                # Calculate P&L
                raw_qty = closed_qty * display_lot_size if exchange != 'MCX' else closed_qty
                if position.direction == 'Long':
                    pnl = (avg_exit_price - position.entry_price) * pnl_multiplier * raw_qty
                else:
                    pnl = (position.entry_price - avg_exit_price) * pnl_multiplier * raw_qty

                # Total fees = proportional entry fees + exit fees
                fees_ratio = closed_qty / position.quantity
                total_fees = (position.fees * fees_ratio) + exit_fees

                # Determine outcome
                if pnl > 0:
                    outcome = 'Win'
                elif pnl < 0:
                    outcome = 'Loss'
                else:
                    outcome = 'Breakeven'

                # Determine timeframe
                if position.entry_date == exit_time.date():
                    timeframe = 'Intraday'
                else:
                    timeframe = 'Positional'

                exit_source_ids = [
                    self._source_id_for_order(match['order'])
                    for match in matched_orders
                ]
                closed_journal_key = build_journal_key(
                    account=self.account,
                    symbol=position.symbol,
                    direction=position.direction,
                    entry_date=position.entry_date,
                    instrument_type=instrument_type,
                    expiry_date=position.expiry_date,
                    option_type=position.option_type,
                    option_strike=position.option_strike,
                    entry_source_ids=self._position_entry_source_ids(position),
                    exit_source_ids=exit_source_ids,
                )

                if position_qty_to_close == 0:
                    # Full close - update existing Notion entry
                    logger.info(f"Closing position {position.page_id}: {position.symbol} {position.direction} P&L={pnl:.2f}")
                    self._update_existing_position(
                        position=position,
                        exit_date=exit_time.date(),
                        exit_time=exit_time_text,
                        exit_price=avg_exit_price,
                        pnl=pnl,
                        fees=total_fees,
                        status='Closed',
                        outcome=outcome,
                        timeframe=timeframe,
                        quantity=closed_qty,
                        journal_key=closed_journal_key,
                    )
                    self.updated_existing_positions.append(position.page_id)
                    closed_positions.append(position)
                else:
                    # Partial close - update existing entry with closed portion
                    logger.info(f"Partially closing position {position.page_id}: {closed_qty}/{position.quantity}")
                    self._update_existing_position(
                        position=position,
                        exit_date=exit_time.date(),
                        exit_time=exit_time_text,
                        exit_price=avg_exit_price,
                        pnl=pnl,
                        fees=total_fees,
                        status='Partially Filled',
                        outcome=outcome,
                        timeframe=timeframe,
                        quantity=closed_qty,
                        journal_key=closed_journal_key,
                    )
                    self.updated_existing_positions.append(position.page_id)
                    closed_positions.append(position)

                    # Create new entry for remaining open quantity
                    remaining_qty = position_qty_to_close
                    remaining_fees = position.fees * (remaining_qty / position.quantity)

                    remaining_entry = TradeEntry(
                        symbol=parsed.base_symbol,
                        direction=position.direction,
                        entry_date=position.entry_date,
                        entry_time=position.entry_time,
                        entry_price=position.entry_price,
                        quantity=remaining_qty,
                        instrument_type=instrument_type,
                        lot_size=display_lot_size,
                        fees=round(remaining_fees, 2),
                        stop_loss=position.stop_loss,
                        exit_date=None,
                        exit_time=None,
                        exit_price=None,
                        pnl=0.0,
                        outcome='Open',
                        timeframe='Positional',
                        status='Open',
                        expiry_date=position.expiry_date,
                        option_type=position.option_type,
                        option_strike=position.option_strike,
                        entry_source_ids=self._position_entry_source_ids(position),
                    )
                    self._ensure_trade_journal_key(remaining_entry)
                    trade_entries.append(remaining_entry)

        # Build remaining orders list - orders with remaining quantity
        remaining_orders = []
        for oq in order_queue:
            if oq['remaining_qty'] > 0:
                # If order was partially used, we need to handle it carefully
                # For now, we skip partially-used orders to avoid double-counting
                # Only include orders that were not used at all
                if oq['remaining_qty'] == oq['original_qty']:
                    remaining_orders.append(oq['order'])
                else:
                    # Partially used order - log warning
                    # The remaining quantity won't create new positions
                    # (This handles edge cases like over-selling)
                    logger.warning(
                        f"Order {oq['order'].order_id} partially used: "
                        f"{oq['original_qty'] - oq['remaining_qty']}/{oq['original_qty']} qty consumed"
                    )

        return used_orders, remaining_orders, closed_positions

    def _position_belongs_to_account(self, position: OpenPosition) -> bool:
        if self.account == 'BALA':
            return not position.label.startswith('[')
        return position.label.startswith(f'[{self.account}]')

    @staticmethod
    def _source_id_for_order(order: Order) -> str:
        return str(order.trade_id or order.order_id)

    @staticmethod
    def _position_entry_source_ids(position: OpenPosition) -> list[str]:
        existing = extract_source_ids(position.journal_key, 'entry_ids')
        return existing or [f"CARRY:{position.page_id}"]

    def _ensure_trade_journal_key(self, trade: TradeEntry) -> None:
        if trade.journal_key:
            return
        trade.journal_key = build_journal_key(
            account=self.account,
            symbol=trade.symbol,
            direction=trade.direction,
            entry_date=trade.entry_date,
            instrument_type=trade.instrument_type,
            expiry_date=trade.expiry_date,
            option_type=trade.option_type,
            option_strike=trade.option_strike,
            entry_source_ids=trade.entry_source_ids,
            exit_source_ids=trade.exit_source_ids,
        )

    def _update_existing_position(
        self,
        position: OpenPosition,
        exit_date: date,
        exit_time: str,
        exit_price: float,
        pnl: float,
        fees: float,
        status: str,
        outcome: str,
        timeframe: str,
        quantity: Optional[int] = None,
        journal_key: Optional[str] = None,
    ) -> None:
        if self.dry_run:
            logger.info(
                "[DRY RUN] Would update existing position %s: %s %s -> Status=%s Exit=%.2f P&L=%.2f Fees=%.2f Qty=%s JournalKey=%s",
                position.page_id,
                position.symbol,
                position.direction,
                status,
                exit_price,
                pnl,
                fees,
                quantity if quantity is not None else position.quantity,
                journal_key or position.journal_key,
            )
            return

        self.notion.update_trade_entry(
            page_id=position.page_id,
            exit_date=exit_date,
            exit_time=exit_time,
            exit_price=exit_price,
            pnl=pnl,
            fees=fees,
            status=status,
            outcome=outcome,
            timeframe=timeframe,
            quantity=quantity,
            journal_key=journal_key,
        )

    def _annotate_option_adjustments(self, trade_entries: list[TradeEntry]) -> None:
        """
        Mark newly added option legs as spread adjustments when they are added
        to an already-open same-symbol/same-expiry option campaign.

        This keeps P&L/quantity math leg-based, but prevents journal rows from
        reading like unrelated standalone trades.
        """
        open_option_entries = [
            trade for trade in trade_entries
            if trade.status == 'Open'
            and trade.option_type is not None
            and trade.expiry_date is not None
            and trade.instrument_type in ('Equity Options', 'Index Options', 'Commodity Options')
        ]
        if len(open_option_entries) < 2:
            return

        grouped: dict[tuple[str, date], list[TradeEntry]] = defaultdict(list)
        for trade in open_option_entries:
            grouped[(trade.symbol, trade.expiry_date)].append(trade)

        for (symbol, expiry_date), entries in grouped.items():
            if len(entries) < 2:
                continue

            existing_positions = [
                pos for pos in self.open_positions
                if self._position_belongs_to_account(pos)
                and pos.symbol == symbol
                and pos.expiry_date == expiry_date
                and pos.option_type is not None
            ]
            if len(existing_positions) < 2:
                continue

            existing_desc = ", ".join(
                f"{'+' if pos.direction == 'Long' else '-'}{pos.option_strike:g}{pos.option_type[:1]}"
                for pos in sorted(existing_positions, key=lambda item: item.option_strike or 0)
            )
            new_desc = ", ".join(
                f"{'+' if item.direction == 'Long' else '-'}{item.option_strike:g}{item.option_type[:1]} @ {item.entry_price}"
                for item in sorted(entries, key=lambda item: item.option_strike or 0)
            )
            note = (
                f"Adjustment leg, not a standalone fresh trade. Existing open {symbol} options structure: "
                f"{existing_desc}. New legs added today: {new_desc}. Intent: adjust the existing spread into a "
                "modified butterfly / spread adjustment, lock partial profits, improve net debit or breakeven, "
                "and keep the added short option risk hedged."
            )
            for trade in entries:
                trade.strategy = trade.strategy or 'Discretionary'
                trade.pre_trade_notes = trade.pre_trade_notes or note
                trade.post_trade_review = trade.post_trade_review or note
                trade.followed_plan = True

    def _process_single_instrument(
        self,
        trading_symbol: str,
        orders: list[Order],
        stop_loss: Optional[float] = None
    ) -> list[TradeEntry]:
        """Process orders for a single trading symbol into trade entries.

        Each unique trading symbol (including strike/expiry) is treated as a separate instrument.
        If only buys OR only sells exist → open position (Positional timeframe).
        If both buys AND sells exist → match as closed trade(s).
        """
        # Separate buys and sells
        buys = [o for o in orders if o.transaction_type == 'BUY']
        sells = [o for o in orders if o.transaction_type == 'SELL']

        # Parse the trading symbol for instrument details
        parsed = self.upstox.parse_trading_symbol(trading_symbol)

        # Get instrument details from Upstox (for instrument_key used in fee calculation)
        inst_details = self.upstox.get_instrument_details(
            trading_symbol,
            orders[0].exchange,
            orders[0].instrument_token
        )

        # Get contract multiplier for P&L calculation
        exchange = orders[0].exchange
        if exchange == 'MCX':
            pnl_multiplier = CONTRACT_SPECS.get(parsed.base_symbol, {}).get('multiplier', 1)
        else:
            pnl_multiplier = 1

        display_lot_size = self._display_lot_size(exchange, parsed, inst_details)

        # Get instrument token for fee calculation
        instrument_token = self._resolve_fee_instrument_token(
            exchange,
            parsed,
            inst_details,
            orders[0].instrument_token
        )

        expiry_date = self._normalized_expiry_date(parsed, inst_details)

        # Determine instrument type based on exchange and parsed instrument type
        is_index = self._is_index_symbol(parsed.base_symbol)
        if exchange == 'MCX':
            if parsed.is_option:
                instrument_type = 'Commodity Options'
            else:
                instrument_type = 'Commodity Futures'
        elif exchange in ('NSE', 'NFO', 'NSE_FO', 'BSE', 'BFO', 'BSE_FO'):
            if parsed.is_option:
                instrument_type = 'Index Options' if is_index else 'Equity Options'
            elif parsed.instrument_type == 'FUT':
                instrument_type = 'Index Futures' if is_index else 'Equity Futures'
            else:
                instrument_type = 'Equity'
        else:
            instrument_type = 'Equity'

        trade_entries = []

        # Case 1: Only one direction → Open position
        if not buys or not sells:
            # All orders are same direction
            direction = 'Long' if buys else 'Short'
            position_orders = buys if buys else sells

            # Calculate aggregated entry
            total_qty = sum(o.quantity for o in position_orders)
            avg_price = sum(o.quantity * o.average_price for o in position_orders) / total_qty
            entry_time = min(o.order_timestamp for o in position_orders)
            entry_time_text = self._time_text_from_orders(position_orders, prefer='min')

            # Calculate fees
            if instrument_token:
                fees = self.upstox.calculate_brokerage(
                    instrument_token, total_qty,
                    'BUY' if direction == 'Long' else 'SELL',
                    avg_price
                )
            else:
                fees = 25.0 * len(position_orders)  # Fallback estimate

            # Create open position entry
            display_qty = self._display_quantity(total_qty, exchange, parsed, inst_details)
            trade_entry = TradeEntry(
                symbol=parsed.base_symbol,
                direction=direction,
                entry_date=entry_time.date(),
                entry_time=entry_time_text,
                entry_price=round(avg_price, 2),
                quantity=display_qty,
                instrument_type=instrument_type,
                lot_size=display_lot_size,
                fees=round(fees, 2),
                raw_quantity=total_qty,
                stop_loss=stop_loss,
                # Exit fields are None for open positions
                exit_date=None,
                exit_time=None,
                exit_price=None,
                pnl=0.0,
                outcome='Open',
                timeframe='Positional',  # Open positions are positional
                status='Open',
                expiry_date=expiry_date,
                option_type=parsed.option_type,
                option_strike=parsed.strike,
                entry_source_ids=[self._source_id_for_order(order) for order in position_orders],
            )
            self._ensure_trade_journal_key(trade_entry)
            trade_entries.append(trade_entry)
            return trade_entries

        # Case 2: Both buys and sells → process chronologically, allowing the
        # net position to flatten and flip intraday.
        orders_by_time = sorted(
            orders,
            key=lambda item: (
                item.order_timestamp,
                item.order_id,
                item.trade_id or item.order_id,
            ),
        )

        order_fees_per_unit: dict[str, float] = {}
        for order in orders_by_time:
            if instrument_token:
                total_fees = self.upstox.calculate_brokerage(
                    instrument_token,
                    order.quantity,
                    order.transaction_type,
                    order.average_price,
                )
            else:
                total_fees = 25.0
            order_fees_per_unit[order.order_id] = total_fees / order.quantity if order.quantity else 0.0

        open_direction: Optional[str] = None
        entry_queue: list[dict] = []

        for order in orders_by_time:
            order_direction = 'Long' if order.transaction_type == 'BUY' else 'Short'
            order_time_text = self._order_time_text(order)
            qty_remaining = order.quantity

            # Flat book or same-side add: this order opens/adds to the current leg.
            if open_direction is None or order_direction == open_direction:
                open_direction = order_direction
                entry_queue.append({
                    'remaining_qty': qty_remaining,
                    'price': order.average_price,
                    'timestamp': order.order_timestamp,
                    'time_text': order_time_text,
                    'order_id': order.order_id,
                    'trade_id': self._source_id_for_order(order),
                    'fees_per_unit': order_fees_per_unit[order.order_id],
                })
                continue

            # Opposite-side order: first close existing FIFO entries.
            while qty_remaining > 0 and entry_queue:
                entry = entry_queue[0]
                match_qty = min(qty_remaining, entry['remaining_qty'])

                if open_direction == 'Long':
                    pnl = (order.average_price - entry['price']) * pnl_multiplier * match_qty
                else:
                    pnl = (entry['price'] - order.average_price) * pnl_multiplier * match_qty

                tranche_fees = (
                    entry['fees_per_unit'] * match_qty +
                    order_fees_per_unit[order.order_id] * match_qty
                )

                if pnl > 0:
                    outcome = 'Win'
                elif pnl < 0:
                    outcome = 'Loss'
                else:
                    outcome = 'Breakeven'

                timeframe = 'Intraday' if entry['timestamp'].date() == order.order_timestamp.date() else 'Positional'
                display_match_qty = self._display_quantity(match_qty, exchange, parsed, inst_details)

                trade_entry = TradeEntry(
                    symbol=parsed.base_symbol,
                    direction=open_direction,
                    entry_date=entry['timestamp'].date(),
                    entry_time=entry['time_text'],
                    exit_date=order.order_timestamp.date(),
                    exit_time=order_time_text,
                    entry_price=round(entry['price'], 2),
                    exit_price=round(order.average_price, 2),
                    stop_loss=stop_loss,
                    target=round(order.average_price, 2),
                    quantity=display_match_qty,
                    instrument_type=instrument_type,
                    lot_size=display_lot_size,
                    pnl=round(pnl, 2),
                    fees=round(tranche_fees, 2),
                    raw_quantity=match_qty,
                    outcome=outcome,
                    timeframe=timeframe,
                    status='Closed',
                    expiry_date=expiry_date,
                    option_type=parsed.option_type,
                    option_strike=parsed.strike,
                    entry_source_ids=[entry['trade_id']],
                    exit_source_ids=[self._source_id_for_order(order)],
                )
                self._ensure_trade_journal_key(trade_entry)
                trade_entries.append(trade_entry)

                qty_remaining -= match_qty
                entry['remaining_qty'] -= match_qty
                if entry['remaining_qty'] <= 0:
                    entry_queue.pop(0)

            # If the order over-closes the prior leg, the remainder flips into a
            # fresh position in the new direction.
            if qty_remaining > 0:
                open_direction = order_direction
                entry_queue = [{
                    'remaining_qty': qty_remaining,
                    'price': order.average_price,
                    'timestamp': order.order_timestamp,
                    'time_text': order_time_text,
                    'order_id': order.order_id,
                    'trade_id': self._source_id_for_order(order),
                    'fees_per_unit': order_fees_per_unit[order.order_id],
                }]
            elif not entry_queue:
                open_direction = None

        # Any remaining queue entries are still-open positions.
        for entry in entry_queue:
            if entry['remaining_qty'] <= 0:
                continue

            remaining_qty = entry['remaining_qty']
            remaining_fees = entry['fees_per_unit'] * remaining_qty
            display_remaining_qty = self._display_quantity(remaining_qty, exchange, parsed, inst_details)
            open_entry = TradeEntry(
                symbol=parsed.base_symbol,
                direction=open_direction or 'Long',
                entry_date=entry['timestamp'].date(),
                entry_time=entry['time_text'],
                entry_price=round(entry['price'], 2),
                quantity=display_remaining_qty,
                instrument_type=instrument_type,
                lot_size=display_lot_size,
                fees=round(remaining_fees, 2),
                raw_quantity=remaining_qty,
                stop_loss=stop_loss,
                exit_date=None,
                exit_time=None,
                exit_price=None,
                pnl=0.0,
                outcome='Open',
                timeframe='Positional',
                status='Open',
                expiry_date=expiry_date,
                option_type=parsed.option_type,
                option_strike=parsed.strike,
                entry_source_ids=[entry['trade_id']],
            )
            self._ensure_trade_journal_key(open_entry)
            trade_entries.append(open_entry)

        return trade_entries

    def create_journal_entries(
        self,
        trade_entries: list[TradeEntry],
        dry_run: bool = False
    ) -> list[dict]:
        """Create Notion journal entries for processed trades."""
        results = []

        for i, trade in enumerate(trade_entries):
            self._ensure_trade_journal_key(trade)
            # Generate label (include account name if not primary BALA account)
            trade_date_str = trade.entry_date.strftime('%b %d')
            account_prefix = f"[{self.account}] " if self.account != 'BALA' else ""
            if len(trade_entries) > 1:
                label = f"{account_prefix}{trade.symbol} {trade.direction} T{i+1} - {trade_date_str}"
            else:
                label = f"{account_prefix}{trade.symbol} {trade.direction} - {trade_date_str}"

            if dry_run:
                action, fields = self.notion.preview_trade_entry_action(trade, label)
                if action == "create":
                    logger.info(f"[DRY RUN] Would create: {label}")
                elif action == "update":
                    logger.info(f"[DRY RUN] Would update: {label} | Fields: {', '.join(fields)}")
                else:
                    logger.info(f"[DRY RUN] Would skip unchanged row: {label}")

                logger.info(f"  Entry: {trade.entry_price} @ {trade.entry_time}")
                if trade.is_open:
                    logger.info(f"  Status: OPEN ({trade.timeframe})")
                else:
                    logger.info(f"  Exit: {trade.exit_price} @ {trade.exit_time}")
                    logger.info(f"  P&L: {trade.pnl}, Fees: {trade.fees}")
                results.append({'label': label, 'status': f'dry_run_{action}', 'fields': fields})
            else:
                logger.info(f"Upserting entry: {label}")
                result = self.notion.upsert_trade_entry(trade, label)
                action = result.get("status", "updated")
                fields = result.get("updated_fields", [])
                results.append({
                    'id': result.get('id'),
                    'label': label,
                    'status': action,
                    'fields': fields,
                })
                if action == "created":
                    logger.info(f"  Created: {result.get('id')}")
                elif action == "updated":
                    logger.info(f"  Updated: {result.get('id')} | Fields: {', '.join(fields)}")
                else:
                    logger.info(f"  Skipped unchanged row: {result.get('id')}")

        return results


SUPPORTED_ACCOUNTS = ['BALA', 'NIMMY']


def main():
    parser = argparse.ArgumentParser(
        description='Automate trade journaling from Upstox to Notion'
    )
    parser.add_argument(
        '--env-file',
        default=str(DEFAULT_ENV_FILE),
        help='Path to .env file'
    )
    parser.add_argument(
        '--account',
        type=str,
        choices=SUPPORTED_ACCOUNTS,
        default='BALA',
        help='Upstox account to use (BALA or NIMMY). Default: BALA'
    )
    parser.add_argument(
        '--date',
        type=str,
        help='Process trades for specific date (YYYY-MM-DD). Default: today'
    )
    parser.add_argument(
        '--stop-loss',
        type=float,
        help='Stop loss price (optional, for R-multiple calculation)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview trades without creating Notion entries'
    )
    parser.add_argument(
        '--notify',
        action='store_true',
        help='Send macOS notification on completion'
    )
    args = parser.parse_args()

    target_date = None
    if args.date:
        try:
            target_date = date.fromisoformat(args.date)
        except ValueError:
            logger.error("Invalid --date value. Use YYYY-MM-DD format.")
            return 1

    logger.info("=" * 60)
    logger.info(f"Trade Journaling Started at {datetime.now()}")
    logger.info(f"Account: {args.account}")
    if target_date:
        logger.info(f"Trade Date: {target_date.isoformat()}")
    logger.info("=" * 60)

    # Load environment
    load_dotenv(args.env_file)

    # Get account-specific credentials
    account = args.account.upper()
    upstox_token = os.getenv(f'UPSTOX_{account}_ACCESS_TOKEN')

    # Fallback to legacy env var for BALA account
    if not upstox_token and account == 'BALA':
        upstox_token = os.getenv('UPSTOX_ACCESS_TOKEN')

    notion_key = os.getenv('NOTION_API_KEY')
    notion_db = os.getenv('NOTION_TRADING_JOURNAL_DB')

    if not upstox_token:
        logger.error(f"UPSTOX_{account}_ACCESS_TOKEN not found in environment")
        return 1

    if not notion_key or not notion_db:
        logger.error("NOTION_API_KEY or NOTION_TRADING_JOURNAL_DB not found")
        return 1

    # Strip quotes if present
    upstox_token = upstox_token.strip("'\"")
    notion_key = notion_key.strip("'\"")
    notion_db = notion_db.strip("'\"")

    try:
        # Initialize clients
        upstox = UpstoxClient(upstox_token)
        notion = NotionClient(notion_key, notion_db)
        processor = TradeProcessor(upstox, notion, account=account, dry_run=args.dry_run)

        # Fetch orders
        fetch_label = target_date.isoformat() if target_date else "today"
        logger.info("Fetching completed orders for %s...", fetch_label)
        orders = upstox.get_completed_orders(target_date=target_date)

        if not orders:
            logger.info("No completed orders found for %s", fetch_label)
            return 0

        logger.info(f"Found {len(orders)} completed orders")
        for order in orders:
            logger.info(
                f"  {order.transaction_type} {order.quantity} "
                f"{order.trading_symbol} @ {order.average_price}"
            )

        # Process into trade entries
        logger.info("\nProcessing orders into trade entries...")
        trade_entries = processor.process_orders(orders, args.stop_loss)
        run_summary = processor.last_run_summary or processor._build_run_summary(trade_entries)

        logger.info(
            "Activity Type: %s | %s",
            run_summary.get('activity_label', 'Unknown'),
            run_summary.get('activity_detail', '')
        )
        logger.info(
            "Activity Counts: updated_existing=%s, new_entries=%s, new_open=%s, new_closed=%s",
            run_summary.get('updated_existing_positions', 0),
            run_summary.get('new_trade_entries', 0),
            run_summary.get('new_open_entries', 0),
            run_summary.get('new_closed_entries', 0),
        )

        if not trade_entries:
            if processor.updated_existing_positions:
                logger.info(
                    "No new trade rows were needed. Updated %s existing Notion position(s).",
                    len(processor.updated_existing_positions)
                )
                logger.info("\n" + "=" * 60)
                logger.info("SUMMARY")
                logger.info("=" * 60)
                logger.info(f"Activity Type: {run_summary.get('activity_label', 'Closing day')}")
                logger.info(f"Updated Existing Positions: {len(processor.updated_existing_positions)}")
                logger.info("New Entries Created: 0")
                logger.info("=" * 60)
                return 0

            logger.warning("Could not process orders into valid trade entries")
            return 1

        logger.info(f"\nCreated {len(trade_entries)} trade entries:")
        for entry in trade_entries:
            net_pnl = entry.pnl - entry.fees
            logger.info(
                f"  {entry.symbol} {entry.direction}: "
                f"P&L={entry.pnl:.2f}, Fees={entry.fees:.2f}, "
                f"Net={net_pnl:.2f}"
            )

        # Create Notion entries
        logger.info("\nCreating Notion journal entries...")
        results = processor.create_journal_entries(trade_entries, args.dry_run)

        # Summary
        total_pnl = sum(e.pnl for e in trade_entries)
        total_fees = sum(e.fees for e in trade_entries)
        net_pnl = total_pnl - total_fees

        logger.info("\n" + "=" * 60)
        logger.info("SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Activity Type: {run_summary.get('activity_label', 'Unknown')}")
        logger.info(f"Total Entries: {len(trade_entries)}")
        logger.info(f"Updated Existing Positions: {run_summary.get('updated_existing_positions', 0)}")
        logger.info(f"New Open Entries: {run_summary.get('new_open_entries', 0)}")
        logger.info(f"New Closed Entries: {run_summary.get('new_closed_entries', 0)}")
        logger.info(f"Total P&L: {total_pnl:.2f}")
        logger.info(f"Total Fees: {total_fees:.2f}")
        logger.info(f"Net P&L: {net_pnl:.2f}")
        logger.info("=" * 60)

        if args.notify:
            send_notification(
                "Trade Journaling Complete",
                f"{len(trade_entries)} trades logged. Net P&L: {net_pnl:.2f}",
                success=True
            )

        return 0

    except Exception as e:
        logger.error(f"Trade journaling FAILED: {e}")
        import traceback
        traceback.print_exc()

        if args.notify:
            send_notification(
                "Trade Journaling Failed",
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
