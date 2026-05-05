#!/usr/bin/env python3
"""
Repair or recover Notion trading-journal rows from broker XLSX exports.

Typical workflows:

1. Same-day journal already exists, but exact historical times are missing.
   Run this script to patch Entry Time / Exit Time and reconcile direction/labels
   where needed.
2. An entire day or week was missed.
   Run this script on the broker export to repair existing rows and create any
   missing rows safely using the stable Journal Key.

This script is intentionally dependency-light and parses the XLSX file with the
standard library so it can run even when openpyxl is unavailable.
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests
from dotenv import load_dotenv

from journal_keys import JOURNAL_KEY_PROPERTY, build_journal_key
from trade_journaling import (
    NotionClient as JournalNotionClient,
    Order as JournalOrder,
    TradeProcessor as JournalTradeProcessor,
    UpstoxClient as JournalUpstoxClient,
)


LOG_FILE = os.path.expanduser('~/Library/Logs/broker_trade_backfill.log')
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


NS = {'a': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
EXCEL_EPOCH = date(1899, 12, 30)
ACCOUNT_BY_UCC = {
}
INDEX_SYMBOLS = {
    'NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'NIFTYNXT50',
    'SENSEX', 'BANKEX'
}
SYMBOL_ALIASES = {
    'TATASTL': 'TATASTEEL',
    'ADANIPORTSLTD': 'ADANIPORTS',
    'ADANIPORTS': 'ADANIPORTS',
    'ETERNALLIMITED': 'ETERNAL',
    'ETERNAL': 'ETERNAL',
    'TATASTEEL': 'TATASTEEL',
}


@dataclass
class BrokerTradeFill:
    account: str
    ucc: str
    trade_date: date
    symbol: str
    exchange: str
    segment: str
    option_type: Optional[str]
    strike: Optional[float]
    expiry_date: Optional[date]
    side: str  # Buy or Sell
    quantity_raw: int
    price: float
    trade_num: str
    trade_time: str

    @property
    def instrument_type(self) -> str:
        if self.segment == 'COM':
            return 'Commodity Options' if self.option_type else 'Commodity Futures'
        if self.segment == 'FO':
            if self.option_type:
                return 'Index Options' if self.symbol in INDEX_SYMBOLS else 'Equity Options'
            return 'Index Futures' if self.symbol in INDEX_SYMBOLS else 'Equity Futures'
        return 'Equity'


@dataclass
class NotionTradeRow:
    page_id: str
    label: str
    symbol: str
    direction: str
    entry_date: Optional[date]
    entry_time: str
    entry_price: float
    exit_date: Optional[date]
    exit_time: str
    exit_price: Optional[float]
    pnl: float
    status: str
    outcome: Optional[str]
    timeframe: str
    option_type: Optional[str]
    strike: Optional[float]
    expiry_date: Optional[date]
    quantity: int
    lot_size: int
    account: str
    instrument_type: str
    journal_key: Optional[str]


@dataclass
class ExpectedJournalTrade:
    symbol: str
    instrument_type: str
    option_type: Optional[str]
    strike: Optional[float]
    expiry_date: Optional[date]
    direction: str
    entry_date: date
    entry_time: str
    entry_price: float
    quantity: int
    status: str
    timeframe: str
    exit_date: Optional[date] = None
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    entry_source_ids: list[str] = field(default_factory=list)
    exit_source_ids: list[str] = field(default_factory=list)


class NotionClient:
    BASE_URL = "https://api.notion.com/v1"

    def __init__(self, api_key: str, database_id: str):
        self.database_id = database_id
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
            "Content-Type": "application/json",
        }
        self._journal_key_checked = False
        self._journal_key_enabled = False

    def _get_database(self) -> dict:
        url = f"{self.BASE_URL}/databases/{self.database_id}"
        response = requests.get(url, headers=self.headers, timeout=30)
        response.raise_for_status()
        return response.json()

    def ensure_journal_key_property(self) -> bool:
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
                "Notion property '%s' exists with type=%s, not rich_text. "
                "Backfill journal-key writes are disabled until corrected.",
                JOURNAL_KEY_PROPERTY,
                prop.get("type"),
            )
            self._journal_key_checked = True
            self._journal_key_enabled = False
            return False

        url = f"{self.BASE_URL}/databases/{self.database_id}"
        payload = {"properties": {JOURNAL_KEY_PROPERTY: {"rich_text": {}}}}
        response = requests.patch(url, headers=self.headers, json=payload, timeout=30)
        response.raise_for_status()
        logger.info("Created Notion property '%s' for stable journal dedupe.", JOURNAL_KEY_PROPERTY)
        self._journal_key_checked = True
        self._journal_key_enabled = True
        return True

    def query_rows_for_date(self, target_date: date) -> list[dict]:
        url = f"{self.BASE_URL}/databases/{self.database_id}/query"
        payload = {
            "page_size": 100,
            "filter": {
                "or": [
                    {"property": "Entry Date", "date": {"equals": target_date.isoformat()}},
                    {"property": "Exit Date", "date": {"equals": target_date.isoformat()}},
                ]
            }
        }

        results = []
        next_cursor = None
        while True:
            if next_cursor:
                payload["start_cursor"] = next_cursor
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            body = response.json()
            results.extend(body.get("results", []))
            if not body.get("has_more"):
                break
            next_cursor = body.get("next_cursor")

        return results

    def query_rows_for_symbol(self, symbol: str) -> list[dict]:
        url = f"{self.BASE_URL}/databases/{self.database_id}/query"
        payload = {
            "page_size": 100,
            "filter": {
                "property": "Symbol",
                "rich_text": {"equals": symbol},
            }
        }

        results = []
        next_cursor = None
        while True:
            if next_cursor:
                payload["start_cursor"] = next_cursor
            response = requests.post(url, headers=self.headers, json=payload, timeout=30)
            response.raise_for_status()
            body = response.json()
            results.extend(body.get("results", []))
            if not body.get("has_more"):
                break
            next_cursor = body.get("next_cursor")

        return results

    def update_page_fields(self, page_id: str, properties: dict) -> dict:
        url = f"{self.BASE_URL}/pages/{page_id}"
        response = requests.patch(url, headers=self.headers, json={"properties": properties}, timeout=30)
        response.raise_for_status()
        return response.json()


def _plain_text_value(prop: dict) -> str:
    if not prop:
        return ""
    prop_type = prop.get("type")
    if prop_type == "title":
        return "".join(part.get("plain_text", "") for part in prop.get("title", []))
    if prop_type == "rich_text":
        return "".join(part.get("plain_text", "") for part in prop.get("rich_text", []))
    return ""


def _select_value(prop: dict) -> Optional[str]:
    select = (prop or {}).get("select")
    return select.get("name") if select else None


def _date_value(prop: dict) -> Optional[date]:
    value = (prop or {}).get("date")
    if not value or not value.get("start"):
        return None
    return datetime.fromisoformat(value["start"]).date()


def _number_value(prop: dict) -> Optional[float]:
    return (prop or {}).get("number")


def _row_account(label: str) -> str:
    if label.startswith('['):
        end = label.find(']')
        if end > 1:
            return label[1:end]
    return 'BALA'


def parse_notion_rows(raw_rows: list[dict], account: str) -> list[NotionTradeRow]:
    rows: list[NotionTradeRow] = []
    for page in raw_rows:
        props = page["properties"]
        label = _plain_text_value(props.get("Trade Label"))
        row_account = _row_account(label)
        if row_account != account:
            continue

        rows.append(NotionTradeRow(
            page_id=page["id"],
            label=label,
            symbol=_plain_text_value(props.get("Symbol")),
            direction=_select_value(props.get("Direction")) or "",
            entry_date=_date_value(props.get("Entry Date")),
            entry_time=_plain_text_value(props.get("Entry Time")) or "",
            entry_price=float(_number_value(props.get("Entry Price")) or 0.0),
            exit_date=_date_value(props.get("Exit Date")),
            exit_time=_plain_text_value(props.get("Exit Time")) or "",
            exit_price=_number_value(props.get("Exit Price")),
            pnl=float(_number_value(props.get("P&L")) or 0.0),
            status=_select_value(props.get("Status")) or "",
            outcome=_select_value(props.get("Outcome")),
            timeframe=_select_value(props.get("Timeframe")) or "",
            option_type=_select_value(props.get("Option Type")),
            strike=_number_value(props.get("Option Strike")),
            expiry_date=_date_value(props.get("Expiry Date")),
            quantity=int(_number_value(props.get("Quantity")) or 0),
            lot_size=int(_number_value(props.get("Lot Size")) or 1),
            account=row_account,
            instrument_type=_select_value(props.get("Instrument Type")) or "",
            journal_key=_plain_text_value(props.get(JOURNAL_KEY_PROPERTY)) or None,
        ))
    return rows


def cell_value(cell: ET.Element) -> str:
    cell_type = cell.get('t')
    if cell_type == 'inlineStr':
        return ''.join((node.text or '') for node in cell.findall('.//a:t', NS))
    value = cell.find('a:v', NS)
    return value.text if value is not None else ''


def excel_serial_to_date(value: str) -> date:
    return EXCEL_EPOCH + timedelta(days=int(float(value)))


def parse_ddmmyyyy(value: str) -> Optional[date]:
    if not value:
        return None
    return datetime.strptime(value, '%d-%m-%Y').date()


def load_account_by_ucc() -> dict[str, str]:
    mapping: dict[str, str] = {}
    for account in ('BALA', 'NIMMY'):
        ucc = (os.getenv(f'BROKER_UCC_{account}') or '').strip()
        if ucc:
            mapping[ucc] = account
    return mapping


def normalize_symbol(company: str, scrip_code: str, segment: str, exchange: str) -> str:
    if scrip_code and scrip_code.isalpha() and segment in ('FO', 'COM'):
        base = re.sub(r'[^A-Z0-9]', '', scrip_code.upper())
        if base:
            return SYMBOL_ALIASES.get(base, base)

    raw = company.upper()
    raw = raw.replace(' LIMITED', '')
    raw = raw.replace(' LTD', '')
    raw = raw.replace('.', '')
    raw = raw.replace('&', '')
    raw = re.sub(r'[^A-Z0-9]', '', raw)
    return SYMBOL_ALIASES.get(raw, raw)


def parse_option_type(value: str) -> Optional[str]:
    text = (value or '').lower()
    if 'call' in text:
        return 'Call'
    if 'put' in text:
        return 'Put'
    return None


def parse_trade_time(value: str) -> datetime.time:
    text = (value or '').strip()
    for fmt in ('%H:%M:%S', '%H:%M'):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Unsupported trade time: {value}")


def broker_fill_to_exchange(fill: BrokerTradeFill) -> str:
    exchange = (fill.exchange or "").upper()
    segment = (fill.segment or "").upper()
    if exchange in ('FON', 'NFO'):
        return 'NFO'
    if exchange in ('FOB', 'BFO'):
        return 'BFO'
    if exchange == 'NSE' and segment == 'FO':
        return 'NFO'
    if exchange == 'BSE' and segment == 'FO':
        return 'BFO'
    return exchange or segment or 'NSE'


def broker_fill_to_trading_symbol(fill: BrokerTradeFill) -> str:
    if fill.segment == 'EQ':
        return f"{fill.symbol}-EQ"

    if not fill.expiry_date:
        return fill.symbol

    suffix = fill.expiry_date.strftime('%d%b%y').upper()
    if fill.option_type:
        strike = int(fill.strike) if fill.strike is not None and float(fill.strike).is_integer() else fill.strike
        return f"{fill.symbol}{suffix}{strike}{fill.option_type[:1].upper()}E".replace("CALL", "CE").replace("PUT", "PE")
    return f"{fill.symbol}{suffix}FUT"


def fills_to_journal_orders(fills: list[BrokerTradeFill]) -> list[JournalOrder]:
    orders: list[JournalOrder] = []
    for fill in fills:
        trade_dt = datetime.combine(fill.trade_date, parse_trade_time(fill.trade_time))
        option_code = None
        if fill.option_type:
            option_code = 'CE' if fill.option_type == 'Call' else 'PE'
        normalized_fill = BrokerTradeFill(
            account=fill.account,
            ucc=fill.ucc,
            trade_date=fill.trade_date,
            symbol=fill.symbol,
            exchange=fill.exchange,
            segment=fill.segment,
            option_type=option_code,
            strike=fill.strike,
            expiry_date=fill.expiry_date,
            side=fill.side,
            quantity_raw=fill.quantity_raw,
            price=fill.price,
            trade_num=fill.trade_num,
            trade_time=fill.trade_time,
        )
        orders.append(JournalOrder(
            order_id=fill.trade_num,
            trade_id=fill.trade_num,
            trading_symbol=broker_fill_to_trading_symbol(normalized_fill),
            transaction_type=fill.side.upper(),
            quantity=fill.quantity_raw,
            average_price=fill.price,
            order_timestamp=trade_dt,
            exchange=broker_fill_to_exchange(fill),
            instrument_token=None,
            time_text=fill.trade_time,
        ))
    return sorted(orders, key=lambda order: (order.order_timestamp, order.order_id))


def parse_broker_trade_file(path: str, account_override: Optional[str] = None, target_date: Optional[date] = None) -> list[BrokerTradeFill]:
    with zipfile.ZipFile(path) as workbook:
        root = ET.fromstring(workbook.read('xl/worksheets/sheet1.xml'))

    sheet_data = root.find('a:sheetData', NS)
    if sheet_data is None:
        raise RuntimeError(f"No sheetData found in {path}")

    rows = []
    ucc = None
    for row in sheet_data.findall('a:row', NS):
        row_num = int(row.get('r'))
        values = {re.sub(r'\d+', '', cell.get('r')): cell_value(cell) for cell in row.findall('a:c', NS)}
        if row_num == 4:
            ucc = values.get('B')
        rows.append((row_num, values))

    if not ucc:
        raise RuntimeError(f"Could not find UCC in broker file: {path}")

    account = account_override or load_account_by_ucc().get(ucc)
    if not account:
        raise RuntimeError(
            f"Unknown UCC {ucc} in {path}. Pass --account explicitly or set BROKER_UCC_BALA / BROKER_UCC_NIMMY in the env file."
        )

    fills: list[BrokerTradeFill] = []
    for row_num, values in rows:
        if row_num < 11:
            continue
        if values.get('J') is None:
            continue
        if values.get('K') is None or values.get('L') is None:
            continue

        trade_date = excel_serial_to_date(values['A']) if values.get('A') else None
        if target_date and trade_date != target_date:
            continue

        option_type = parse_option_type(values.get('G', ''))
        strike = float(values['H']) if values.get('H') not in (None, '') else None
        expiry_date = parse_ddmmyyyy(values.get('I', ''))
        side = values['L'].strip().title()
        symbol = normalize_symbol(values.get('B', ''), values.get('F', ''), values.get('E', ''), values.get('D', ''))

        fills.append(BrokerTradeFill(
            account=account,
            ucc=ucc,
            trade_date=trade_date,
            symbol=symbol,
            exchange=values.get('D', ''),
            segment=values.get('E', ''),
            option_type=option_type,
            strike=strike,
            expiry_date=expiry_date,
            side=side,
            quantity_raw=int(float(values['M'])),
            price=float(values['N']),
            trade_num=values['J'],
            trade_time=values['K'],
        ))

    return fills


def price_equal(left: Optional[float], right: Optional[float], tol: float = 0.005) -> bool:
    if left is None or right is None:
        return False
    return abs(float(left) - float(right)) < tol


def strike_equal(left: Optional[float], right: Optional[float]) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return abs(float(left) - float(right)) < 0.005


def expiry_matches(left: Optional[date], right: Optional[date], symbol: str) -> bool:
    if left == right:
        return True
    if not left or not right:
        return False
    if (
        symbol not in INDEX_SYMBOLS and
        left.year == right.year and
        left.month == right.month and
        abs(left.day - right.day) <= 3 and
        max(left.day, right.day) >= 25
    ):
        return True
    return False


def lots_from_fill(fill: BrokerTradeFill, row: NotionTradeRow) -> int:
    lot_size = max(int(row.lot_size or 1), 1)
    if fill.exchange == 'MCX':
        return fill.quantity_raw
    return max(1, int(round(fill.quantity_raw / lot_size)))


def desired_side(row: NotionTradeRow, phase: str) -> str:
    if phase == 'entry':
        return 'Buy' if row.direction == 'Long' else 'Sell'
    return 'Sell' if row.direction == 'Long' else 'Buy'


def row_price_for_phase(row: NotionTradeRow, phase: str) -> Optional[float]:
    return row.entry_price if phase == 'entry' else row.exit_price


def row_date_for_phase(row: NotionTradeRow, phase: str) -> Optional[date]:
    return row.entry_date if phase == 'entry' else row.exit_date


def row_time_for_phase(row: NotionTradeRow, phase: str) -> str:
    return row.entry_time if phase == 'entry' else row.exit_time


def row_needs_update(row: NotionTradeRow, phase: str, target_date: date) -> bool:
    row_date = row_date_for_phase(row, phase)
    if row_date != target_date:
        return False
    time_value = (row_time_for_phase(row, phase) or '').strip()
    return time_value in ('', '00:00', 'N/A')


def fill_matches_row(fill: BrokerTradeFill, row: NotionTradeRow, phase: str) -> bool:
    if fill.trade_date != row_date_for_phase(row, phase):
        return False
    if fill.side != desired_side(row, phase):
        return False
    if fill.symbol != row.symbol:
        return False
    if fill.option_type != row.option_type:
        return False
    if not strike_equal(fill.strike, row.strike):
        return False
    if not expiry_matches(fill.expiry_date, row.expiry_date, row.symbol):
        return False
    if not price_equal(fill.price, row_price_for_phase(row, phase)):
        return False
    return True


def row_key(row: NotionTradeRow) -> tuple[str, Optional[str], Optional[float], Optional[date]]:
    return (row.symbol, row.option_type, row.strike, row.expiry_date)


def fill_key(fill: BrokerTradeFill) -> tuple[str, Optional[str], Optional[float], Optional[date]]:
    return (fill.symbol, fill.option_type, fill.strike, fill.expiry_date)


def natural_label_sort_key(label: str) -> tuple[int, str]:
    match = re.search(r'T(\d+)', label)
    return (int(match.group(1)) if match else 9999, label)


def cleaned_label(row: NotionTradeRow, direction: str) -> Optional[str]:
    current = row.label or ""
    for token in ("Long", "Short"):
        old_fragment = f"{row.symbol} {token}"
        new_fragment = f"{row.symbol} {direction}"
        if old_fragment in current:
            return current.replace(old_fragment, new_fragment, 1)
    new_fragment = f"{row.symbol} {direction}"
    return None


def build_expected_trades_for_group(fills: list[BrokerTradeFill], lot_size: int) -> list[ExpectedJournalTrade]:
    fills_sorted = sorted(
        fills,
        key=lambda fill: (fill.trade_date, parse_trade_time(fill.trade_time), fill.trade_num)
    )

    open_queue: list[dict] = []
    expected: list[ExpectedJournalTrade] = []

    for fill in fills_sorted:
        lots = max(1, int(round(fill.quantity_raw / lot_size))) if fill.exchange != 'MCX' else fill.quantity_raw
        fill_direction = 'Long' if fill.side == 'Buy' else 'Short'

        while lots > 0 and open_queue and open_queue[0]['direction'] != fill_direction:
            entry = open_queue[0]
            match_qty = min(lots, entry['remaining_qty'])

            expected.append(ExpectedJournalTrade(
                symbol=fill.symbol,
                instrument_type=fill.instrument_type,
                option_type=fill.option_type,
                strike=fill.strike,
                expiry_date=fill.expiry_date,
                direction=entry['direction'],
                entry_date=entry['trade_date'],
                entry_time=entry['trade_time'],
                entry_price=entry['price'],
                quantity=match_qty,
                status='Closed',
                timeframe='Intraday' if entry['trade_date'] == fill.trade_date else 'Positional',
                exit_date=fill.trade_date,
                exit_time=fill.trade_time,
                exit_price=fill.price,
                entry_source_ids=[entry['trade_num']],
                exit_source_ids=[fill.trade_num],
            ))

            entry['remaining_qty'] -= match_qty
            lots -= match_qty
            if entry['remaining_qty'] <= 0:
                open_queue.pop(0)

        if lots > 0:
            open_queue.append({
                'direction': fill_direction,
                'trade_date': fill.trade_date,
                'trade_time': fill.trade_time,
                'price': fill.price,
                'remaining_qty': lots,
                'trade_num': fill.trade_num,
            })

    for remaining in open_queue:
        expected.append(ExpectedJournalTrade(
            symbol=fills_sorted[0].symbol,
            instrument_type=fills_sorted[0].instrument_type,
            option_type=fills_sorted[0].option_type,
            strike=fills_sorted[0].strike,
            expiry_date=fills_sorted[0].expiry_date,
            direction=remaining['direction'],
            entry_date=remaining['trade_date'],
            entry_time=remaining['trade_time'],
            entry_price=remaining['price'],
            quantity=remaining['remaining_qty'],
            status='Open',
            timeframe='Positional',
            entry_source_ids=[remaining['trade_num']],
        ))

    return expected


def compute_expected_pnl(expected: ExpectedJournalTrade, row: NotionTradeRow) -> float:
    if expected.status != 'Closed' or expected.exit_price is None:
        return 0.0
    raw_qty = expected.quantity * max(int(row.lot_size or 1), 1)
    if expected.direction == 'Long':
        return round((expected.exit_price - expected.entry_price) * raw_qty, 2)
    return round((expected.entry_price - expected.exit_price) * raw_qty, 2)


def compute_outcome(pnl: float, status: str) -> str:
    if status == 'Open':
        return 'Open'
    if pnl > 0:
        return 'Win'
    if pnl < 0:
        return 'Loss'
    return 'Breakeven'


def build_reconciliation_updates(rows: list[NotionTradeRow], fills: list[BrokerTradeFill], target_date: date) -> dict[str, dict]:
    rows_by_key: dict[tuple[str, Optional[str], Optional[float], Optional[date]], list[NotionTradeRow]] = {}
    for row in rows:
        if row.entry_date != target_date and row.exit_date != target_date:
            continue
        rows_by_key.setdefault(row_key(row), []).append(row)

    fills_by_key: dict[tuple[str, Optional[str], Optional[float], Optional[date]], list[BrokerTradeFill]] = {}
    for fill in fills:
        fills_by_key.setdefault(fill_key(fill), []).append(fill)

    updates: dict[str, dict] = {}
    for key, fill_group in fills_by_key.items():
        candidate_rows = rows_by_key.get(key, [])
        if not candidate_rows:
            continue

        candidate_rows = sorted(candidate_rows, key=lambda row: natural_label_sort_key(row.label))
        lot_size = max(int(candidate_rows[0].lot_size or 1), 1)
        expected_trades = build_expected_trades_for_group(fill_group, lot_size)

        if len(expected_trades) != len(candidate_rows):
            logger.warning(
                "Expected/row count mismatch for %s: expected=%s notion=%s",
                key, len(expected_trades), len(candidate_rows)
            )

        for row, expected in zip(candidate_rows, expected_trades):
            props = {}
            journal_key = build_journal_key(
                account=row.account,
                symbol=expected.symbol,
                direction=expected.direction,
                entry_date=expected.entry_date,
                instrument_type=expected.instrument_type,
                expiry_date=expected.expiry_date,
                option_type=expected.option_type,
                option_strike=expected.strike,
                entry_source_ids=expected.entry_source_ids,
                exit_source_ids=expected.exit_source_ids,
            )
            new_label = cleaned_label(row, expected.direction)
            if new_label and new_label != row.label:
                props["Trade Label"] = {"title": [{"text": {"content": new_label}}]}
            if row.journal_key != journal_key:
                props[JOURNAL_KEY_PROPERTY] = {"rich_text": [{"text": {"content": journal_key}}]}
            if row.direction != expected.direction:
                props["Direction"] = {"select": {"name": expected.direction}}
            if row.entry_date != expected.entry_date:
                props["Entry Date"] = {"date": {"start": expected.entry_date.isoformat()}}
            if row.entry_time != expected.entry_time:
                props["Entry Time"] = {"rich_text": [{"text": {"content": expected.entry_time}}]}
            if not price_equal(row.entry_price, expected.entry_price):
                props["Entry Price"] = {"number": expected.entry_price}
            if row.quantity != expected.quantity:
                props["Quantity"] = {"number": expected.quantity}
            if row.status != expected.status:
                props["Status"] = {"select": {"name": expected.status}}
            if row.timeframe != expected.timeframe:
                props["Timeframe"] = {"select": {"name": expected.timeframe}}

            if expected.status == 'Closed':
                if row.exit_date != expected.exit_date:
                    props["Exit Date"] = {"date": {"start": expected.exit_date.isoformat()}}
                if row.exit_time != (expected.exit_time or ''):
                    props["Exit Time"] = {"rich_text": [{"text": {"content": expected.exit_time or ''}}]}
                if not price_equal(row.exit_price, expected.exit_price):
                    props["Exit Price"] = {"number": expected.exit_price}
                pnl = compute_expected_pnl(expected, row)
                outcome = compute_outcome(pnl, expected.status)
                if not price_equal(row.pnl, pnl):
                    props["P&L"] = {"number": pnl}
                if row.outcome != outcome:
                    props["Outcome"] = {"select": {"name": outcome}}
                if not price_equal(row.exit_price, expected.exit_price):
                    props["Target"] = {"number": expected.exit_price}

            if props:
                updates[row.page_id] = props

    return updates


def assign_times(rows: list[NotionTradeRow], fills: list[BrokerTradeFill], phase: str, target_date: date) -> dict[str, str]:
    candidates = [row for row in rows if row_needs_update(row, phase, target_date)]
    candidates.sort(key=lambda row: (row.symbol, row.label))

    pools: dict[int, int] = {}
    assigned: dict[str, str] = {}

    for row in candidates:
        lots_needed = max(int(row.quantity or 1), 1)
        consumed_times: list[str] = []

        for idx, fill in enumerate(fills):
            if not fill_matches_row(fill, row, phase):
                continue

            available = pools.get(idx)
            if available is None:
                available = lots_from_fill(fill, row)

            if available <= 0:
                continue

            take = min(available, lots_needed)
            if take <= 0:
                continue

            pools[idx] = available - take
            lots_needed -= take
            consumed_times.extend([fill.trade_time] * take)

            if lots_needed <= 0:
                break

        if consumed_times:
            assigned[row.page_id] = consumed_times[0] if phase == 'entry' else consumed_times[-1]
        else:
            logger.warning("Could not match %s time for %s", phase, row.label)

    return assigned


def build_time_updates(rows: list[NotionTradeRow], fills: list[BrokerTradeFill], target_date: date) -> dict[str, dict]:
    updates: dict[str, dict] = {}
    entry_updates = assign_times(rows, fills, 'entry', target_date)
    exit_updates = assign_times(rows, fills, 'exit', target_date)

    for row in rows:
        properties = {}
        entry_time = entry_updates.get(row.page_id)
        exit_time = exit_updates.get(row.page_id)
        if entry_time and entry_time != row.entry_time:
            properties["Entry Time"] = {"rich_text": [{"text": {"content": entry_time}}]}
        if exit_time and exit_time != row.exit_time:
            properties["Exit Time"] = {"rich_text": [{"text": {"content": exit_time}}]}
        if properties:
            updates[row.page_id] = properties

    return updates


def filter_unsafe_historical_open_creates(
    notion: NotionClient,
    account: str,
    trade_entries: list,
    target_date: date,
) -> list:
    """Drop synthetic 'open' creations that are really historical closes/adjustments.

    When replaying an old date after the original prior-day position is already
    closed in Notion, the live open-position lookup no longer has enough state
    to distinguish a close-only day from a fresh-open day. We protect against
    that by checking symbol history in Notion before allowing a new historical
    open row to be created.
    """
    symbol_cache: dict[str, list[NotionTradeRow]] = {}
    safe_entries = []

    for trade in trade_entries:
        if getattr(trade, 'status', '') != 'Open' or getattr(trade, 'entry_date', None) != target_date:
            safe_entries.append(trade)
            continue

        symbol = getattr(trade, 'symbol', '')
        if symbol not in symbol_cache:
            symbol_rows = parse_notion_rows(notion.query_rows_for_symbol(symbol), account)
            symbol_cache[symbol] = symbol_rows

        active_prior_rows = [
            row for row in symbol_cache[symbol]
            if row.symbol == trade.symbol
            and row.entry_date is not None
            and row.entry_date < target_date
            and row.direction != getattr(trade, 'direction', '')
            and row.option_type == getattr(trade, 'option_type', None)
            and strike_equal(row.strike, getattr(trade, 'option_strike', None))
            and expiry_matches(row.expiry_date, getattr(trade, 'expiry_date', None), trade.symbol)
            and (row.exit_date is None or row.exit_date >= target_date)
        ]

        if active_prior_rows:
            logger.info(
                "Skipping historical open-create for %s on %s because %s prior active row(s) already exist in Notion.",
                trade.symbol,
                target_date,
                len(active_prior_rows),
            )
            continue

        safe_entries.append(trade)

    return safe_entries


def replay_fills_through_trade_journal(
    *,
    notion: NotionClient,
    notion_key: str,
    notion_db: str,
    account: str,
    fills: list[BrokerTradeFill],
    dry_run: bool,
    env_file: str,
) -> tuple[int, int]:
    upstox_token = os.getenv(f'UPSTOX_{account}_ACCESS_TOKEN')
    if not upstox_token and account == 'BALA':
        upstox_token = os.getenv('UPSTOX_ACCESS_TOKEN')
    if not upstox_token:
        logger.warning("Skipping broker-create fallback for %s: missing access token.", account)
        return 0, 0

    journal_notion = JournalNotionClient(notion_key.strip("'\""), notion_db.strip("'\""))
    upstox = JournalUpstoxClient(
        upstox_token.strip("'\""),
        account=account,
        env_file=env_file,
    )
    processor = JournalTradeProcessor(upstox, journal_notion, account=account, dry_run=dry_run)
    orders = fills_to_journal_orders(fills)
    trade_entries = processor.process_orders(orders)
    trade_date = min(fill.trade_date for fill in fills)
    trade_entries = filter_unsafe_historical_open_creates(notion, account, trade_entries, trade_date)
    results = processor.create_journal_entries(trade_entries, dry_run=dry_run)

    changed = 0
    created = 0
    for result in results:
        status = result.get('status', '')
        if status.endswith('create') or status == 'created':
            changed += 1
            created += 1
        elif status.endswith('update') or status == 'updated':
            changed += 1
    changed += len(processor.updated_existing_positions)
    return changed, created


def run_backfill(
    notion: NotionClient,
    broker_file: str,
    account_override: Optional[str],
    target_date: Optional[date],
    dry_run: bool,
    notion_key: str,
    notion_db: str,
    env_file: str,
) -> tuple[str, int, int]:
    fills = parse_broker_trade_file(broker_file, account_override=account_override, target_date=target_date)
    if not fills:
        logger.warning("No fills found in %s for %s", broker_file, target_date.isoformat() if target_date else "all dates")
        return account_override or "UNKNOWN", 0, 0

    account = fills[0].account
    notion.ensure_journal_key_property()
    fills_by_date: dict[date, list[BrokerTradeFill]] = {}
    for fill in fills:
        fills_by_date.setdefault(fill.trade_date, []).append(fill)

    if dry_run and len(fills_by_date) > 1:
        logger.warning(
            "Dry-run across multiple dates is approximate for positional carry-forward, "
            "because preview mode does not persist each day's journal state before the next day is simulated."
        )

    total_changed = 0
    total_created = 0

    for trade_date in sorted(fills_by_date):
        day_fills = fills_by_date[trade_date]
        logger.info("Broker file %s -> account=%s date=%s fills=%s", broker_file, account, trade_date, len(day_fills))
        raw_rows = notion.query_rows_for_date(trade_date)
        rows = parse_notion_rows(raw_rows, account)
        logger.info("Found %s Notion rows for %s on %s", len(rows), account, trade_date)

        updates = build_reconciliation_updates(rows, day_fills, trade_date)
        time_only_updates = build_time_updates(rows, day_fills, trade_date)
        for page_id, properties in time_only_updates.items():
            updates.setdefault(page_id, {}).update(properties)

        updated_count = 0
        if updates:
            for row in rows:
                props = updates.get(row.page_id)
                if not props:
                    continue
                if dry_run:
                    logger.info("[DRY RUN] Would update %s (%s): %s", row.label, row.page_id, list(props.keys()))
                else:
                    notion.update_page_fields(row.page_id, props)
                    logger.info("Updated %s (%s): %s", row.label, row.page_id, list(props.keys()))
                updated_count += 1
        else:
            logger.info("No direct row repairs required for %s on %s", broker_file, trade_date)

        journal_changed, journal_created = replay_fills_through_trade_journal(
            notion=notion,
            notion_key=notion_key,
            notion_db=notion_db,
            account=account,
            fills=day_fills,
            dry_run=dry_run,
            env_file=env_file,
        )
        total_changed += updated_count + journal_changed
        total_created += journal_created

    return account, total_changed, total_created


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair or recover Notion trade journal rows from Upstox broker XLSX exports.")
    parser.add_argument('--broker-file', action='append', required=True, help='Path to broker XLSX export. Repeat for multiple files.')
    parser.add_argument('--date', help='Trade date to process in YYYY-MM-DD. Defaults to all dates found in each file.')
    parser.add_argument('--account', choices=['BALA', 'NIMMY'], help='Override account detection from UCC.')
    parser.add_argument('--env-file', default=str(DEFAULT_ENV_FILE), help='Path to .env file containing Notion credentials.')
    parser.add_argument('--dry-run', action='store_true', help='Preview updates without writing to Notion.')
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    load_dotenv(args.env_file)

    notion_key = (os.getenv('NOTION_API_KEY') or '').strip("'\"")
    notion_db = (os.getenv('NOTION_TRADING_JOURNAL_DB') or '').strip("'\"")
    if not notion_key or not notion_db:
        logger.error("NOTION_API_KEY or NOTION_TRADING_JOURNAL_DB not found")
        return 1

    target_date = date.fromisoformat(args.date) if args.date else None
    notion = NotionClient(notion_key, notion_db)

    total_updates = 0
    total_created = 0
    for broker_file in args.broker_file:
        _, count, created = run_backfill(
            notion=notion,
            broker_file=broker_file,
            account_override=args.account,
            target_date=target_date,
            dry_run=args.dry_run,
            notion_key=notion_key,
            notion_db=notion_db,
            env_file=args.env_file,
        )
        total_updates += count
        total_created += created

    logger.info("Completed broker trade backfill. Changed rows: %s | Created rows: %s", total_updates, total_created)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
