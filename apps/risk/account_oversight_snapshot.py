#!/usr/bin/env python3.11
from __future__ import annotations

"""
Daily account oversight snapshot.

This is intentionally read-only. It pulls broker cash/margin, open positions,
and realized/unrealized P&L, then stores an audit trail for BALA and NIMMY.
"""

import argparse
import csv
import json
import logging
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from mtm_guard import (
    ENV_FILE,
    RISK_RUNTIME_ROOT,
    SUPPORTED_ACCOUNTS,
    TelegramClient,
    UpstoxRiskClient,
    env_values,
    format_money,
    load_alert_chat_id,
    load_telegram_token,
    now_local,
)


LOG_FILE = os.path.expanduser("~/Library/Logs/account_oversight_snapshot.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Store a read-only broker account cash/P&L snapshot.")
    parser.add_argument("--account", choices=(*SUPPORTED_ACCOUNTS, "ALL"), default="ALL")
    parser.add_argument("--env-file", default=str(ENV_FILE))
    parser.add_argument("--state-dir", default=str(RISK_RUNTIME_ROOT))
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Snapshot directory. Defaults to <state-dir>/account_snapshots.",
    )
    parser.add_argument("--send-telegram", action="store_true", help="Send a concise account snapshot to Telegram.")
    parser.add_argument("--disable-telegram-send", action="store_true", help="Log Telegram payload locally.")
    return parser.parse_args()


def _number(payload: dict[str, Any], *path: str) -> float:
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return 0.0
        current = current.get(key)
    try:
        return float(current or 0.0)
    except (TypeError, ValueError):
        return 0.0


def summarize_funds(account: str, funds: dict[str, Any]) -> dict[str, Any]:
    available = funds.get("available_to_trade") or {}
    unavailable = funds.get("unavailable_to_trade") or {}
    cash_bucket = available.get("cash_available_to_trade") or {}
    pledge_bucket = available.get("pledge_available_to_trade") or {}
    cash_margin_used = cash_bucket.get("margin_used") or {}
    pledge_margin_used = pledge_bucket.get("margin_used") or {}
    cash = cash_bucket.get("cash") or {}
    pledge = pledge_bucket.get("margin_from_pledge") or {}
    unsettled = ((unavailable.get("cash_unavailable_to_trade") or {}).get("unsettled_profit") or {})
    unavailable_pledge = unavailable.get("pledge_unavailable_to_trade") or {}

    return {
        "account": account,
        "available_total": _number(available, "total"),
        "cash_available": _number(cash_bucket, "total"),
        "cash_opening_balance": _number(cash, "opening_balance"),
        "cash_added_today": _number(cash, "added_today"),
        "cash_withdrawn_today": _number(cash, "withdrawn_today"),
        "stock_sale_cash": _number(cash, "amount_from_stock_sale"),
        "unpaid_charges": _number(cash, "unpaid_charges"),
        "cash_margin_used_total": _number(cash_margin_used, "total"),
        "cash_span_exposure": _number(cash_margin_used, "span_exposure"),
        "cash_premium_present": _number(cash_margin_used, "premium_present"),
        "cash_margin_loss_total": _number(cash_margin_used, "loss", "total"),
        "cash_margin_loss_realised": _number(cash_margin_used, "loss", "realised"),
        "cash_margin_loss_unrealised": _number(cash_margin_used, "loss", "unrealised"),
        "pledge_available": _number(pledge_bucket, "total"),
        "pledge_total": _number(pledge, "total"),
        "pledge_equity": _number(pledge, "equity"),
        "pledge_mutual_funds": _number(pledge, "mutual_funds"),
        "pledge_margin_used_total": _number(pledge_margin_used, "total"),
        "pledge_span_exposure": _number(pledge_margin_used, "span_exposure"),
        "pledge_premium_present": _number(pledge_margin_used, "premium_present"),
        "unsettled_todays_profit": _number(unsettled, "todays_profit"),
        "unsettled_previous_days": _number(unsettled, "previous_days"),
        "pledge_unavailable_equity": _number(unavailable_pledge, "equity"),
        "pledge_unavailable_mutual_funds": _number(unavailable_pledge, "mutual_funds"),
    }


def latest_path(output_dir: Path, account: str) -> Path:
    return output_dir / f"account_snapshot_latest_{account.lower()}.json"


def load_previous_summary(output_dir: Path, account: str) -> dict[str, Any]:
    path = latest_path(output_dir, account)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("summary") or {}
    except Exception:
        return {}


def build_account_snapshot(account: str, env_file: Path, output_dir: Path) -> dict[str, Any]:
    client = UpstoxRiskClient(env_file=env_file, account=account)
    previous = load_previous_summary(output_dir, account)
    captured_at = now_local().isoformat()
    funds = client.get_funds_and_margin()
    mtm = client.build_snapshot()
    funds_summary = summarize_funds(account, funds)
    summary = {
        **funds_summary,
        "captured_at": captured_at,
        "net_pnl": mtm.net_pnl,
        "realised_pnl": mtm.realised_pnl,
        "unrealised_pnl": mtm.unrealised_pnl,
        "open_positions": mtm.open_positions,
        "closed_positions_today": mtm.closed_positions_today,
        "available_total_change": round(
            funds_summary["available_total"] - float(previous.get("available_total") or 0.0),
            2,
        ) if previous else None,
        "cash_available_change": round(
            funds_summary["cash_available"] - float(previous.get("cash_available") or 0.0),
            2,
        ) if previous else None,
    }
    return {
        "account": account,
        "captured_at": captured_at,
        "summary": summary,
        "funds_margin_raw": funds,
        "mtm_snapshot": {
            **asdict(mtm),
            "positions": [asdict(position) for position in mtm.positions],
        },
    }


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def append_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames = [
        "captured_at",
        "account",
        "available_total",
        "cash_available",
        "pledge_available",
        "pledge_total",
        "cash_margin_used_total",
        "pledge_margin_used_total",
        "net_pnl",
        "realised_pnl",
        "unrealised_pnl",
        "open_positions",
        "closed_positions_today",
        "available_total_change",
        "cash_available_change",
    ]
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        if not exists:
            writer.writeheader()
        for row in rows:
            writer.writerow(row)


def persist_snapshots(output_dir: Path, snapshots: list[dict[str, Any]]) -> dict[str, str]:
    output_dir.mkdir(parents=True, exist_ok=True)
    day = now_local().date().isoformat()
    jsonl_path = output_dir / f"account_snapshots_{day}.jsonl"
    csv_path = output_dir / "account_balance_history.csv"
    latest_all_path = output_dir / "account_snapshot_latest.json"

    rows = []
    for snapshot in snapshots:
        append_jsonl(jsonl_path, snapshot)
        latest_path(output_dir, snapshot["account"]).write_text(
            json.dumps(snapshot, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        rows.append(snapshot["summary"])

    append_csv(csv_path, rows)
    latest_all_path.write_text(
        json.dumps(
            {
                "captured_at": now_local().isoformat(),
                "accounts": {snapshot["account"]: snapshot for snapshot in snapshots},
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return {
        "jsonl": str(jsonl_path),
        "csv": str(csv_path),
        "latest": str(latest_all_path),
    }


def render_summary(snapshots: list[dict[str, Any]], paths: dict[str, str]) -> str:
    lines = [
        "*Account Oversight Snapshot*",
        f"Captured: `{now_local().strftime('%Y-%m-%d %H:%M:%S IST')}`",
        "",
    ]
    for snapshot in snapshots:
        summary = snapshot["summary"]
        mtm_positions = snapshot["mtm_snapshot"]["positions"]
        delta = summary.get("available_total_change")
        delta_text = f" | change `{format_money(float(delta))}`" if delta is not None else ""
        lines.extend(
            [
                f"*{summary['account']}*",
                f"Available total: `{format_money(summary['available_total'])}`{delta_text}",
                f"Cash available: `{format_money(summary['cash_available'])}` | Pledge available: `{format_money(summary['pledge_available'])}`",
                f"Margin used: cash `{format_money(summary['cash_margin_used_total'])}` | pledge `{format_money(summary['pledge_margin_used_total'])}`",
                f"P&L: net `{format_money(summary['net_pnl'])}` | realised `{format_money(summary['realised_pnl'])}` | unrealised `{format_money(summary['unrealised_pnl'])}`",
                f"Open positions: `{summary['open_positions']}` | Closed rows today: `{summary['closed_positions_today']}`",
            ]
        )
        if mtm_positions:
            for position in mtm_positions[:4]:
                lines.append(
                    f"- `{position['symbol']}` | qty `{position['quantity']}` | "
                    f"LTP `{float(position['last_price']):.2f}` | P&L `{format_money(float(position['pnl']))}`"
                )
            remaining = int(summary["open_positions"]) - min(4, len(mtm_positions))
            if remaining > 0:
                lines.append(f"- ... plus `{remaining}` more open position(s)")
        lines.append("")
    lines.append(f"Stored: `{paths['latest']}`")
    return "\n".join(lines).strip()


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file)
    state_dir = Path(args.state_dir)
    output_dir = Path(args.output_dir) if args.output_dir else state_dir / "account_snapshots"
    accounts = SUPPORTED_ACCOUNTS if args.account == "ALL" else (args.account,)

    snapshots = []
    errors = []
    for account in accounts:
        try:
            snapshots.append(build_account_snapshot(account, env_file, output_dir))
        except Exception as exc:
            logger.exception("[%s] Failed to build account snapshot: %s", account, exc)
            errors.append({"account": account, "error": str(exc)})

    if not snapshots and errors:
        raise SystemExit(json.dumps({"status": "error", "errors": errors}, indent=2))

    paths = persist_snapshots(output_dir, snapshots)
    summary = render_summary(snapshots, paths)
    print(summary)
    if errors:
        print("\nErrors:")
        print(json.dumps(errors, indent=2))

    if args.send_telegram:
        values = env_values(env_file)
        chat_id = load_alert_chat_id(values)
        if not chat_id:
            raise SystemExit("TELEGRAM_ALERT_CHAT_ID / TELEGRAM_CHAT_ID is missing.")
        telegram = TelegramClient(load_telegram_token(values), disable_send=args.disable_telegram_send)
        telegram.send_message(chat_id, summary)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
