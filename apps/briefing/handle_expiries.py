#!/usr/bin/env python3
"""
Handle Options Expiry Script

Checks for open option positions that have expired and marks them as closed.
For positions without a closing trade, attempts to get the final price from:
1. Upstox P&L API (if available)
2. Manual entry prompt

Usage:
    python handle_expiries.py                    # Check today's expiries
    python handle_expiries.py --date 2026-04-16  # Check specific date
    python handle_expiries.py --dry-run          # Preview without updating

Requirements:
    pip install requests python-dotenv
"""

import os
import sys
import argparse
import logging
from datetime import datetime, date
from pathlib import Path
from typing import Optional
from dotenv import load_dotenv
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / '.env'

# Configure logging
LOG_FILE = os.path.expanduser('~/Library/Logs/handle_expiries.log')
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

    def get_expired_options(self, expiry_date: date) -> list[dict]:
        """Fetch open option positions that expired on the given date."""
        url = f"{self.BASE_URL}/databases/{self.database_id}/query"

        payload = {
            "filter": {
                "and": [
                    {
                        "or": [
                            {"property": "Status", "select": {"equals": "Open"}},
                            {"property": "Status", "select": {"equals": "Partially Filled"}}
                        ]
                    },
                    {
                        "property": "Expiry Date",
                        "date": {"equals": expiry_date.isoformat()}
                    },
                    {
                        "or": [
                            {"property": "Instrument Type", "select": {"equals": "Index Options"}},
                            {"property": "Instrument Type", "select": {"equals": "Equity Options"}},
                            {"property": "Instrument Type", "select": {"equals": "Commodity Options"}}
                        ]
                    }
                ]
            }
        }

        response = requests.post(url, headers=self.headers, json=payload)
        response.raise_for_status()

        data = response.json()
        return data.get('results', [])

    def close_expired_option(
        self,
        page_id: str,
        expiry_date: date,
        final_price: float,
        entry_price: float,
        quantity: int,
        lot_size: int,
        direction: str,
        fees: float,
        instrument_type: str
    ) -> dict:
        """Mark an expired option as closed."""
        # Calculate P&L
        if direction == 'Long':
            pnl = (final_price - entry_price) * quantity * lot_size
        else:
            pnl = (entry_price - final_price) * quantity * lot_size

        # Determine outcome
        if pnl > 0:
            outcome = 'Win'
        elif pnl < 0:
            outcome = 'Loss'
        else:
            outcome = 'Breakeven'

        url = f"{self.BASE_URL}/pages/{page_id}"

        properties = {
            "Exit Date": {"date": {"start": expiry_date.isoformat()}},
            "Exit Time": {"rich_text": [{"text": {"content": "15:30"}}]},  # Standard expiry time
            "Exit Price": {"number": round(final_price, 2)},
            "P&L": {"number": round(pnl, 2)},
            "Status": {"select": {"name": "Closed"}},
            "Outcome": {"select": {"name": outcome}},
            "Target": {"number": round(final_price, 2)}
        }

        payload = {"properties": properties}

        response = requests.patch(url, headers=self.headers, json=payload)
        response.raise_for_status()

        return response.json()


class UpstoxClient:
    """Client for Upstox API interactions."""

    BASE_URL = "https://api.upstox.com/v2"

    def __init__(self, access_token: str):
        self.access_token = access_token
        self.headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json"
        }

    def get_position_pnl(self, instrument_key: str) -> Optional[float]:
        """Try to get P&L for a position from Upstox (if still available)."""
        try:
            url = f"{self.BASE_URL}/portfolio/positions"
            response = requests.get(url, headers=self.headers)
            response.raise_for_status()

            data = response.json()
            if data.get('status') != 'success':
                return None

            # Search for matching position
            positions = data.get('data', [])
            for pos in positions:
                if pos.get('instrument_token') == instrument_key:
                    return pos.get('pnl', {}).get('realised')

        except Exception as e:
            logger.warning(f"Could not fetch P&L from Upstox: {e}")

        return None


def main():
    parser = argparse.ArgumentParser(
        description='Handle expired options and mark them as closed'
    )
    parser.add_argument(
        '--env-file',
        default=str(DEFAULT_ENV_FILE),
        help='Path to .env file'
    )
    parser.add_argument(
        '--date',
        type=str,
        help='Expiry date to process (YYYY-MM-DD). Default: today'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Preview without updating Notion'
    )
    args = parser.parse_args()

    logger.info("=" * 60)
    logger.info(f"Options Expiry Handler Started at {datetime.now()}")
    logger.info("=" * 60)

    # Load environment
    load_dotenv(args.env_file)

    notion_key = os.getenv('NOTION_API_KEY')
    notion_db = os.getenv('NOTION_TRADING_JOURNAL_DB')
    upstox_token = os.getenv('UPSTOX_ACCESS_TOKEN')

    if not notion_key or not notion_db:
        logger.error("NOTION_API_KEY or NOTION_TRADING_JOURNAL_DB not found")
        return 1

    # Strip quotes if present
    notion_key = notion_key.strip("'\"")
    notion_db = notion_db.strip("'\"")

    # Determine expiry date
    if args.date:
        expiry_date = datetime.strptime(args.date, '%Y-%m-%d').date()
    else:
        expiry_date = date.today()

    logger.info(f"Checking for options expiring on {expiry_date}")

    try:
        # Initialize clients
        notion = NotionClient(notion_key, notion_db)
        upstox = None
        if upstox_token:
            upstox_token = upstox_token.strip("'\"")
            upstox = UpstoxClient(upstox_token)

        # Fetch expired options
        expired_options = notion.get_expired_options(expiry_date)

        if not expired_options:
            logger.info("No expired options found for this date")
            return 0

        logger.info(f"Found {len(expired_options)} expired option positions")

        # Process each expired option
        for option in expired_options:
            props = option['properties']

            # Extract trade details
            trade_label_text = props.get('Trade Label', {}).get('title', [])
            trade_label = trade_label_text[0]['plain_text'] if trade_label_text else 'Unknown'

            symbol_text = props.get('Symbol', {}).get('rich_text', [])
            symbol = symbol_text[0]['plain_text'] if symbol_text else ''

            direction = props.get('Direction', {}).get('select', {}).get('name', '')
            entry_price = props.get('Entry Price', {}).get('number', 0.0)
            quantity = int(props.get('Quantity', {}).get('number', 0))
            lot_size = int(props.get('Lot Size', {}).get('number', 1))
            fees = props.get('Fees', {}).get('number', 0.0)
            instrument_type = props.get('Instrument Type', {}).get('select', {}).get('name', '')
            option_strike = props.get('Option Strike', {}).get('number')

            logger.info(f"\n{'='*60}")
            logger.info(f"Processing: {trade_label}")
            logger.info(f"  Symbol: {symbol}")
            logger.info(f"  Direction: {direction}")
            logger.info(f"  Entry Price: {entry_price}")
            logger.info(f"  Quantity: {quantity}")
            logger.info(f"  Strike: {option_strike}")

            # Try to get final price from Upstox P&L
            final_price = None
            if upstox:
                instrument_key = props.get('instrument_key')  # If stored
                if instrument_key:
                    pnl = upstox.get_position_pnl(instrument_key)
                    if pnl is not None:
                        # Derive final price from P&L (reverse calculation)
                        # This is tricky, might need manual entry instead
                        logger.info(f"  Found P&L: {pnl}")

            # If no automatic price, prompt for manual entry
            if final_price is None:
                if args.dry_run:
                    final_price = 0.0  # Dummy value for dry run
                    logger.info(f"  [DRY RUN] Would prompt for final price")
                else:
                    print(f"\nEnter final price for {trade_label} (Strike: {option_strike}):")
                    print(f"  Direction: {direction}, Entry: {entry_price}")
                    while True:
                        try:
                            final_price_input = input("  Final price (or 0 if expired worthless): ")
                            final_price = float(final_price_input)
                            break
                        except ValueError:
                            print("  Invalid input. Please enter a number.")

            logger.info(f"  Final Price: {final_price}")

            if args.dry_run:
                logger.info(f"  [DRY RUN] Would close with P&L calculation")
            else:
                # Close the option
                result = notion.close_expired_option(
                    page_id=option['id'],
                    expiry_date=expiry_date,
                    final_price=final_price,
                    entry_price=entry_price,
                    quantity=quantity,
                    lot_size=lot_size,
                    direction=direction,
                    fees=fees,
                    instrument_type=instrument_type
                )
                logger.info(f"  ✓ Closed successfully: {result.get('id')}")

        logger.info("\n" + "=" * 60)
        logger.info(f"Processed {len(expired_options)} expired options")
        logger.info("=" * 60)

        return 0

    except Exception as e:
        logger.error(f"Expiry handling FAILED: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == '__main__':
    sys.exit(main())
