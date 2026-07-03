#!/usr/bin/env python3
from __future__ import annotations

"""
Weekly broker-vs-Notion reconciliation.

This is a read-only audit. It compares broker trade source IDs against the
stable Journal Key values written to Notion by trade_journaling.py.
"""

import argparse
import json
import logging
import os
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

import requests
from dotenv import load_dotenv

from journal_keys import JOURNAL_KEY_PROPERTY, extract_source_ids
from trade_journaling import DEFAULT_ENV_FILE, Order, UpstoxClient


REPO_ROOT = Path(__file__).resolve().parents[2]
RISK_APP_DIR = REPO_ROOT / "apps" / "risk"
if RISK_APP_DIR.exists():
    sys.path.insert(0, str(RISK_APP_DIR))

from mtm_guard import (  # noqa: E402
    SUPPORTED_ACCOUNTS,
    TelegramClient,
    env_values,
    load_alert_chat_id,
    load_telegram_token,
)


LOG_FILE = os.path.expanduser("~/Library/Logs/weekly_journal_reconciliation.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
logger = logging.getLogger(__name__)


KNOWN_ROOTS = (
    "BANKNIFTY",
    "FINNIFTY",
    "MIDCPNIFTY",
    "NIFTYNXT50",
    "SENSEX",
    "BANKEX",
    "NIFTY",
    "SILVERMIC",
    "SILVERM",
    "SILVER",
    "GOLDM",
    "GOLD",
    "CRUDEOILM",
    "CRUDEOIL",
    "NATGASMINI",
    "NATGAS",
    "ZINCMINI",
    "ZINC",
    "TATASTEEL",
    "SUNPHARMA",
    "RELIANCE",
    "COALINDIA",
    "MANAPPURAM",
    "EMAMI",
)


def _resolve_trading_system_root() -> Path:
    env_root = os.getenv("BALAS_TRADING_SYSTEM_ROOT")
    candidates = []
    if env_root:
        candidates.append(Path(env_root).expanduser())
    candidates.extend(
        [
            Path.home() / "balas-product-os" / "Projects" / "trading-system",
            Path("/Users/rugan/balas-product-os/Projects/trading-system"),
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return REPO_ROOT / "data" / "reports"


TRADING_SYSTEM_ROOT = _resolve_trading_system_root()
DEFAULT_OUTPUT_DIR = TRADING_SYSTEM_ROOT / "reconciliation" / "weekly"


def latest_friday_on_or_before(target: date) -> date:
    days_since_friday = (target.weekday() - 4) % 7
    return target - timedelta(days=days_since_friday)


def parse_args() -> argparse.Namespace:
    default_week_end = latest_friday_on_or_before(date.today())
    parser = argparse.ArgumentParser(description="Reconcile broker trades against Notion journal rows.")
    parser.add_argument("--week-ending", default=default_week_end.isoformat())
    parser.add_argument("--start-date", default=None)
    parser.add_argument("--end-date", default=None)
    parser.add_argument("--account", choices=(*SUPPORTED_ACCOUNTS, "ALL"), default="ALL")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_DIR))
    parser.add_argument("--send-telegram", action="store_true")
    parser.add_argument("--disable-telegram-send", action="store_true")
    return parser.parse_args()


def iter_dates(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def normalize_text(value: Optional[str]) -> str:
    return " ".join((value or "").split())


def plain_text(prop: dict[str, Any], prop_type: str) -> str:
    return "".join(item.get("plain_text", "") for item in (prop or {}).get(prop_type, [])).strip()


def select_value(prop: dict[str, Any]) -> Optional[str]:
    value = (prop or {}).get("select")
    return value.get("name") if value else None


def number_value(prop: dict[str, Any]) -> Optional[float]:
    return (prop or {}).get("number")


def date_value(prop: dict[str, Any]) -> Optional[str]:
    value = (prop or {}).get("date")
    return value.get("start") if value else None


def row_account(label: str) -> str:
    match = re.match(r"^\[([A-Z]+)\]", label or "")
    return match.group(1) if match else "BALA"


def symbol_root(value: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]", "", normalize_text(value).upper())
    for root in sorted(KNOWN_ROOTS, key=len, reverse=True):
        if normalized.startswith(root):
            return root
    match = re.match(r"[A-Z]+", normalized)
    return match.group(0) if match else normalized or "UNKNOWN"


def source_id_for_order(order: Order) -> str:
    return str(order.trade_id or order.order_id)


def load_access_token(account: str) -> str:
    token = os.getenv(f"UPSTOX_{account}_ACCESS_TOKEN")
    if not token and account == "BALA":
        token = os.getenv("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError(f"Missing Upstox access token for {account}")
    return token.strip("'\"")


def query_notion_rows(start_date: date, end_date: date) -> list[dict[str, Any]]:
    notion_key = os.getenv("NOTION_API_KEY")
    notion_db = os.getenv("NOTION_TRADING_JOURNAL_DB")
    if not notion_key or not notion_db:
        raise RuntimeError("NOTION_API_KEY or NOTION_TRADING_JOURNAL_DB is missing.")

    notion_key = notion_key.strip("'\"")
    notion_db = notion_db.strip("'\"")
    headers = {
        "Authorization": f"Bearer {notion_key}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    url = f"https://api.notion.com/v1/databases/{notion_db}/query"
    payload: dict[str, Any] = {
        "filter": {
            "or": [
                {
                    "and": [
                        {"property": "Entry Date", "date": {"on_or_after": start_date.isoformat()}},
                        {"property": "Entry Date", "date": {"on_or_before": end_date.isoformat()}},
                    ]
                },
                {
                    "and": [
                        {"property": "Exit Date", "date": {"on_or_after": start_date.isoformat()}},
                        {"property": "Exit Date", "date": {"on_or_before": end_date.isoformat()}},
                    ]
                },
            ]
        },
        "sorts": [{"property": "Entry Date", "direction": "ascending"}],
        "page_size": 100,
    }

    results: list[dict[str, Any]] = []
    while True:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        body = response.json()
        results.extend(body.get("results", []))
        if not body.get("has_more"):
            break
        payload["start_cursor"] = body.get("next_cursor")
    return [parse_notion_row(page) for page in results]


def parse_notion_row(page: dict[str, Any]) -> dict[str, Any]:
    props = page.get("properties", {})
    label = plain_text(props.get("Trade Label", {}), "title")
    symbol = plain_text(props.get("Symbol", {}), "rich_text")
    journal_key = plain_text(props.get(JOURNAL_KEY_PROPERTY, {}), "rich_text")
    entry_date = date_value(props.get("Entry Date", {}))
    exit_date = date_value(props.get("Exit Date", {}))
    return {
        "page_id": page.get("id"),
        "label": label,
        "account": row_account(label),
        "symbol": symbol,
        "root": symbol_root(symbol),
        "status": select_value(props.get("Status", {})) or "Unknown",
        "entry_date": entry_date,
        "exit_date": exit_date,
        "pnl": float(number_value(props.get("P&L", {})) or 0.0),
        "fees": float(number_value(props.get("Fees", {})) or 0.0),
        "journal_key": journal_key,
        "entry_source_ids": extract_source_ids(journal_key, "entry_ids"),
        "exit_source_ids": extract_source_ids(journal_key, "exit_ids"),
    }


def fetch_broker_orders(accounts: tuple[str, ...], start_date: date, end_date: date, env_file: Path) -> list[dict[str, Any]]:
    broker_rows: list[dict[str, Any]] = []
    for account in accounts:
        client = UpstoxClient(load_access_token(account), account=account, env_file=env_file)
        for trade_date in iter_dates(start_date, end_date):
            try:
                orders = client.get_completed_orders(target_date=trade_date)
            except Exception as exc:
                logger.exception("[%s] Failed to fetch broker trades for %s: %s", account, trade_date, exc)
                broker_rows.append(
                    {
                        "account": account,
                        "trade_date": trade_date.isoformat(),
                        "error": str(exc),
                    }
                )
                continue
            for order in orders:
                broker_rows.append(
                    {
                        "account": account,
                        "trade_date": trade_date.isoformat(),
                        "source_id": source_id_for_order(order),
                        "order_id": order.order_id,
                        "trade_id": order.trade_id,
                        "symbol": order.trading_symbol,
                        "root": symbol_root(order.trading_symbol),
                        "side": order.transaction_type,
                        "quantity": int(order.quantity or 0),
                        "price": float(order.average_price or 0.0),
                        "exchange": order.exchange,
                        "time": order.time_text or order.order_timestamp.strftime("%H:%M"),
                    }
                )
    return broker_rows


def build_notion_source_index(rows: list[dict[str, Any]], start_date: date, end_date: date) -> dict[str, set[str]]:
    by_account: dict[str, set[str]] = {account: set() for account in SUPPORTED_ACCOUNTS}
    for row in rows:
        account = row["account"]
        if account not in by_account:
            by_account[account] = set()
        if row.get("entry_date"):
            entry_date = date.fromisoformat(row["entry_date"])
            if start_date <= entry_date <= end_date:
                by_account[account].update(row.get("entry_source_ids") or [])
        if row.get("exit_date"):
            exit_date = date.fromisoformat(row["exit_date"])
            if start_date <= exit_date <= end_date:
                by_account[account].update(row.get("exit_source_ids") or [])
    return by_account


def find_keyless_fallback(order: dict[str, Any], notion_rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    order_date = order.get("trade_date")
    for row in notion_rows:
        if row["account"] != order["account"]:
            continue
        if row.get("journal_key"):
            continue
        if row.get("root") != order.get("root"):
            continue
        if order_date in {row.get("entry_date"), row.get("exit_date")}:
            return row
    return None


def reconcile(accounts: tuple[str, ...], start_date: date, end_date: date, env_file: Path) -> dict[str, Any]:
    notion_rows_all = query_notion_rows(start_date, end_date)
    notion_rows = [row for row in notion_rows_all if row["account"] in set(accounts)]
    broker_rows_all = fetch_broker_orders(accounts, start_date, end_date, env_file)
    broker_errors = [row for row in broker_rows_all if row.get("error")]
    broker_rows = [row for row in broker_rows_all if not row.get("error")]
    source_index = build_notion_source_index(notion_rows, start_date, end_date)

    matched = []
    matched_keyless = []
    missing = []
    for row in broker_rows:
        if row["source_id"] in source_index.get(row["account"], set()):
            matched.append(row)
            continue
        fallback = find_keyless_fallback(row, notion_rows)
        if fallback:
            matched_keyless.append({**row, "notion_page_id": fallback["page_id"], "notion_label": fallback["label"]})
            continue
        missing.append(row)

    notion_week_source_ids: dict[str, set[str]] = {account: set() for account in accounts}
    for row in notion_rows:
        account = row["account"]
        if row.get("entry_date") and start_date <= date.fromisoformat(row["entry_date"]) <= end_date:
            notion_week_source_ids.setdefault(account, set()).update(row.get("entry_source_ids") or [])
        if row.get("exit_date") and start_date <= date.fromisoformat(row["exit_date"]) <= end_date:
            notion_week_source_ids.setdefault(account, set()).update(row.get("exit_source_ids") or [])

    broker_ids_by_account: dict[str, set[str]] = defaultdict(set)
    for row in broker_rows:
        broker_ids_by_account[row["account"]].add(row["source_id"])

    orphan_notion_source_ids = []
    for account, ids in notion_week_source_ids.items():
        for source_id in sorted(ids - broker_ids_by_account.get(account, set())):
            if source_id.startswith("CARRY:"):
                continue
            orphan_notion_source_ids.append({"account": account, "source_id": source_id})

    keyless_rows = [
        row
        for row in notion_rows
        if not row.get("journal_key")
        and (
            row.get("entry_date")
            and start_date <= date.fromisoformat(row["entry_date"]) <= end_date
            or row.get("exit_date")
            and start_date <= date.fromisoformat(row["exit_date"]) <= end_date
        )
    ]

    by_account = []
    for account in accounts:
        account_broker = [row for row in broker_rows if row["account"] == account]
        account_missing = [row for row in missing if row["account"] == account]
        account_keyless = [row for row in matched_keyless if row["account"] == account]
        account_notion = [row for row in notion_rows if row["account"] == account]
        by_account.append(
            {
                "account": account,
                "broker_fills": len(account_broker),
                "notion_rows": len(account_notion),
                "matched_by_source_id": len([row for row in matched if row["account"] == account]),
                "matched_by_symbol_date_but_keyless": len(account_keyless),
                "missing_broker_fills": len(account_missing),
            }
        )

    status = "clean" if not missing and not broker_errors else "exceptions"
    if matched_keyless or keyless_rows or orphan_notion_source_ids:
        status = "needs_review" if status == "clean" else status

    return {
        "generated_at": datetime.now().astimezone().isoformat(),
        "week_start": start_date.isoformat(),
        "week_end": end_date.isoformat(),
        "accounts": list(accounts),
        "status": status,
        "summary": {
            "broker_fills": len(broker_rows),
            "notion_rows": len(notion_rows),
            "matched_by_source_id": len(matched),
            "matched_by_symbol_date_but_keyless": len(matched_keyless),
            "missing_broker_fills": len(missing),
            "broker_fetch_errors": len(broker_errors),
            "notion_keyless_rows": len(keyless_rows),
            "orphan_notion_source_ids": len(orphan_notion_source_ids),
        },
        "by_account": by_account,
        "missing_broker_fills": missing,
        "matched_keyless_broker_fills": matched_keyless,
        "notion_rows_missing_journal_key": keyless_rows,
        "orphan_notion_source_ids": orphan_notion_source_ids,
        "broker_fetch_errors": broker_errors,
    }


def render_report(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "=" * 100,
        "WEEKLY JOURNAL RECONCILIATION",
        f"Generated: {payload['generated_at']}",
        f"Period: {payload['week_start']} to {payload['week_end']}",
        f"Status: {payload['status'].upper()}",
        "=" * 100,
        "",
        "Summary:",
        f"- Broker fills: {summary['broker_fills']}",
        f"- Notion rows: {summary['notion_rows']}",
        f"- Matched by source ID: {summary['matched_by_source_id']}",
        f"- Matched by symbol/date but missing Journal Key: {summary['matched_by_symbol_date_but_keyless']}",
        f"- Missing broker fills: {summary['missing_broker_fills']}",
        f"- Broker fetch errors: {summary['broker_fetch_errors']}",
        f"- Notion rows missing Journal Key: {summary['notion_keyless_rows']}",
        f"- Orphan Notion source IDs: {summary['orphan_notion_source_ids']}",
        "",
        "By Account:",
    ]
    for item in payload.get("by_account", []):
        lines.append(
            f"- {item['account']}: broker fills {item['broker_fills']} | notion rows {item['notion_rows']} | "
            f"source matches {item['matched_by_source_id']} | keyless matches {item['matched_by_symbol_date_but_keyless']} | "
            f"missing {item['missing_broker_fills']}"
        )

    if payload.get("missing_broker_fills"):
        lines.extend(["", "Missing Broker Fills - Explanation Required:"])
        for row in payload["missing_broker_fills"][:50]:
            lines.append(
                f"- {row['account']} {row['trade_date']} {row['time']} {row['side']} "
                f"{row['quantity']} {row['symbol']} @ {row['price']} | source_id {row['source_id']} | explanation: ______"
            )

    if payload.get("matched_keyless_broker_fills"):
        lines.extend(["", "Matched By Symbol/Date But Journal Key Missing:"])
        for row in payload["matched_keyless_broker_fills"][:30]:
            lines.append(
                f"- {row['account']} {row['trade_date']} {row['symbol']} source_id {row['source_id']} "
                f"likely row `{row['notion_label']}`. Action: patch Journal Key/source IDs."
            )

    if payload.get("notion_rows_missing_journal_key"):
        lines.extend(["", "Notion Rows Missing Journal Key:"])
        for row in payload["notion_rows_missing_journal_key"][:30]:
            lines.append(
                f"- {row['account']} {row.get('entry_date') or '-'}->{row.get('exit_date') or '-'} "
                f"{row['label']} | {row['symbol']} | status {row['status']}"
            )

    if payload.get("broker_fetch_errors"):
        lines.extend(["", "Broker Fetch Errors:"])
        for row in payload["broker_fetch_errors"]:
            lines.append(f"- {row['account']} {row['trade_date']}: {row['error']}")

    lines.extend(
        [
            "",
            "Operating Rule:",
            "- A clean week means every broker fill is represented in Notion by source ID.",
            "- Keyless symbol/date matches are not failures, but they are not audit-grade and should be patched.",
            "- Missing fills need a manual explanation before the weekly review is considered complete.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs(payload: dict[str, Any], output_dir: Path) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    text_path = output_dir / f"weekly_journal_reconciliation_{timestamp}.txt"
    json_path = output_dir / f"weekly_journal_reconciliation_{timestamp}.json"
    latest_text = output_dir / "weekly_journal_reconciliation_latest.txt"
    latest_json = output_dir / "weekly_journal_reconciliation_latest.json"
    report = render_report(payload)
    text_path.write_text(report, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest_text.write_text(report, encoding="utf-8")
    latest_json.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {
        "text": str(text_path),
        "json": str(json_path),
        "latest_text": str(latest_text),
        "latest_json": str(latest_json),
    }


def telegram_summary(payload: dict[str, Any], paths: dict[str, str]) -> str:
    summary = payload["summary"]
    return "\n".join(
        [
            "*Weekly Journal Reconciliation*",
            f"Period: `{payload['week_start']} to {payload['week_end']}`",
            f"Status: `{payload['status'].upper()}`",
            f"Broker fills: `{summary['broker_fills']}` | Notion rows: `{summary['notion_rows']}`",
            f"Missing fills: `{summary['missing_broker_fills']}` | Keyless rows: `{summary['notion_keyless_rows']}`",
            f"Report: `{paths['latest_text']}`",
        ]
    )


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file)
    load_dotenv(env_file, override=True)

    if args.start_date and args.end_date:
        start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    else:
        week_end = datetime.strptime(args.week_ending, "%Y-%m-%d").date()
        start_date = week_end - timedelta(days=4)
        end_date = week_end

    accounts = SUPPORTED_ACCOUNTS if args.account == "ALL" else (args.account,)
    payload = reconcile(tuple(accounts), start_date, end_date, env_file)
    paths = write_outputs(payload, Path(args.output))
    print(render_report(payload))
    logger.info("Reconciliation saved to %s", paths["latest_text"])

    if args.send_telegram:
        values = env_values(env_file)
        chat_id = load_alert_chat_id(values)
        if not chat_id:
            raise SystemExit("TELEGRAM_ALERT_CHAT_ID / TELEGRAM_CHAT_ID is missing.")
        telegram = TelegramClient(load_telegram_token(values), disable_send=args.disable_telegram_send)
        telegram.send_message(chat_id, telegram_summary(payload, paths))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
