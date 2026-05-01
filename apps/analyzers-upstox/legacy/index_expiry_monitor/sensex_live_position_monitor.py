#!/usr/bin/env python3.11
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Any

import requests

REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_FILE_DEFAULT = str(REPO_ROOT / ".env")
OUTPUT_DIR_DEFAULT = str(REPO_ROOT / "data" / "legacy-analyzers" / "index-expiry")
QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
POSITIONS_URL = "https://api.upstox.com/v2/portfolio/short-term-positions"
SUPPORTED_ACCOUNTS = ("BALA", "NIMMY")


@dataclass
class Leg:
    symbol: str
    instrument_token: str
    quantity: int
    sell_price: float
    buy_price: float
    last_price: float
    pnl: float
    unrealised: float
    realised: float
    product: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor actual open NIMMY SENSEX option positions with live risk alerts.")
    parser.add_argument("--env-file", default=ENV_FILE_DEFAULT)
    parser.add_argument("--output-dir", default=OUTPUT_DIR_DEFAULT)
    parser.add_argument("--account", choices=SUPPORTED_ACCOUNTS, default="NIMMY")
    parser.add_argument("--poll-seconds", type=int, default=5)
    parser.add_argument("--max-loss-rupees", type=float, default=2000.0)
    parser.add_argument("--profit-target-rupees", type=float, default=5000.0)
    parser.add_argument("--loss-alert-levels", default="1000,1500,1800,2000")
    parser.add_argument("--profit-alert-levels", default="1000,2500,4000,5000")
    parser.add_argument("--exchange", default="BFO")
    parser.add_argument("--symbol-contains", default="SENSEX")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--disable-telegram-alerts", action="store_true")
    parser.add_argument("--heartbeat-minutes", type=float, default=15.0, help="Send a Telegram status heartbeat every N minutes. Default: 15.")
    parser.add_argument("--market-close-time", default="15:30", help="Stop monitoring at this local market close time. Default: 15:30.")
    return parser.parse_args()


def env_value(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}=(.+)$", text, re.M)
    if not match:
        return None
    return match.group(1).strip().strip('"').strip("'")


def load_access_token(env_text: str, account: str) -> str:
    primary = env_value(env_text, f"UPSTOX_{account}_ACCESS_TOKEN")
    if primary:
        return primary
    if account == "BALA":
        fallback = env_value(env_text, "UPSTOX_ACCESS_TOKEN")
        if fallback:
            return fallback
    raise RuntimeError(f"Missing Upstox access token for account {account}")


def parse_float_list(value: str) -> list[float]:
    result = []
    for part in value.split(","):
        part = part.strip()
        if part:
            result.append(float(part))
    return sorted(result)


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str))


def load_sent_alerts(path: Path) -> set[str]:
    state = read_json(path)
    if not state:
        return set()
    return set(state.get("alerts", {}).get("sent", []))


def load_last_heartbeat_ts(path: Path) -> float | None:
    state = read_json(path)
    if not state:
        return None
    value = state.get("heartbeat", {}).get("last_sent_epoch")
    return float(value) if value is not None else None


def format_money(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def parse_hhmm(value: str) -> dtime:
    hour, minute = value.split(":")
    return dtime(hour=int(hour), minute=int(minute))


def is_market_closed(close_time: dtime) -> bool:
    return datetime.now().time() >= close_time


class UpstoxClient:
    def __init__(self, token: str):
        self.headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    def get_positions(self) -> list[dict[str, Any]]:
        response = requests.get(POSITIONS_URL, headers=self.headers, timeout=20)
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"Position API returned non-success status: {payload}")
        return payload.get("data", [])

    def sensex_spot(self) -> float | None:
        response = requests.get(QUOTE_URL, headers=self.headers, params={"instrument_key": "BSE_INDEX|SENSEX"}, timeout=20)
        response.raise_for_status()
        data = response.json().get("data", {})
        quote = data.get("BSE_INDEX:SENSEX") or data.get("BSE_INDEX|SENSEX")
        if not quote:
            return None
        return float(quote.get("last_price") or 0.0)


def to_leg(row: dict[str, Any]) -> Leg:
    return Leg(
        symbol=str(row.get("trading_symbol") or row.get("tradingsymbol") or ""),
        instrument_token=str(row.get("instrument_token") or ""),
        quantity=int(row.get("quantity") or 0),
        sell_price=float(row.get("sell_price") or 0.0),
        buy_price=float(row.get("buy_price") or 0.0),
        last_price=float(row.get("last_price") or 0.0),
        pnl=float(row.get("pnl") or 0.0),
        unrealised=float(row.get("unrealised") or 0.0),
        realised=float(row.get("realised") or 0.0),
        product=str(row.get("product") or ""),
    )


def open_sensex_legs(rows: list[dict[str, Any]], exchange: str, symbol_contains: str) -> list[Leg]:
    result = []
    for row in rows:
        symbol = str(row.get("trading_symbol") or row.get("tradingsymbol") or "")
        quantity = int(row.get("quantity") or 0)
        if quantity == 0:
            continue
        if row.get("exchange") != exchange:
            continue
        if symbol_contains not in symbol:
            continue
        result.append(to_leg(row))
    return sorted(result, key=lambda leg: leg.symbol)


def action_for_pnl(net_pnl: float, max_loss: float, profit_target: float) -> tuple[str, str]:
    if net_pnl <= -abs(max_loss):
        return "EXIT_NOW", "Max loss breached. Exit the live strangle now."
    if net_pnl >= profit_target:
        return "BOOK_PROFIT_EXIT_NOW", "Profit target reached. Exit/book the live strangle now."
    if net_pnl <= -0.75 * abs(max_loss):
        return "DANGER", "Loss is close to max-loss. Watch continuously."
    if net_pnl <= -0.50 * abs(max_loss):
        return "CAUTION", "Loss is elevated."
    if net_pnl >= 0.80 * profit_target:
        return "PROFIT_NEAR_TARGET", "Profit is close to target."
    return "MONITOR", "Position is inside configured risk limits."


def telegram_credentials(env_text: str) -> tuple[str | None, str | None]:
    return env_value(env_text, "TELEGRAM_BOT_TOKEN"), env_value(env_text, "TELEGRAM_CHAT_ID")


def send_telegram_alert(token: str, chat_id: str, message: str) -> str | None:
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=10,
        )
        response.raise_for_status()
        return None
    except Exception as exc:
        return str(exc)


def build_alerts(
    action: str,
    net_pnl: float,
    loss_levels: list[float],
    profit_levels: list[float],
    sent_alerts: set[str],
) -> tuple[list[str], str | None]:
    triggered: list[tuple[str, str]] = []
    loss_used = max(0.0, -net_pnl)
    for level in loss_levels:
        if loss_used >= level:
            triggered.append((f"loss_{level:g}", f"loss crossed INR {level:g}"))
    for level in profit_levels:
        if net_pnl >= level:
            triggered.append((f"profit_{level:g}", f"profit crossed INR {level:g}"))
    if action in {"CAUTION", "DANGER", "EXIT_NOW", "BOOK_PROFIT_EXIT_NOW", "PROFIT_NEAR_TARGET"}:
        triggered.append((f"action_{action}", f"action is {action}"))

    new_items = [(key, desc) for key, desc in triggered if key not in sent_alerts]
    if not new_items:
        return [], None

    keys = [key for key, _ in new_items]
    descriptions = ", ".join(desc for _, desc in new_items)
    message = "\n".join([
        "SENSEX LIVE POSITION ALERT",
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}",
        f"Action: {action}",
        f"Triggered: {descriptions}",
        f"Net P&L: INR {net_pnl:.2f}",
    ])
    return keys, message


def build_heartbeat_message(
    account: str,
    spot: float | None,
    legs: list[Leg],
    net_pnl: float,
    action: str,
    message: str,
    max_loss: float,
    profit_target: float,
) -> str:
    loss_room = round(net_pnl + abs(max_loss), 2)
    target_room = round(profit_target - net_pnl, 2)
    leg_lines = [
        f"- {leg.symbol}: qty {leg.quantity}, LTP {leg.last_price:.2f}, P&L INR {leg.pnl:.2f}"
        for leg in legs
    ]
    if not leg_lines:
        leg_lines = ["- No open matching SENSEX legs."]
    return "\n".join([
        "SENSEX LIVE POSITION HEARTBEAT",
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}",
        f"Account: {account}",
        f"Spot: {format_money(spot)}",
        f"Action: {action}",
        f"Status: {message}",
        f"Net P&L: INR {net_pnl:.2f}",
        f"Room to max loss: INR {loss_room:.2f}",
        f"Room to profit target: INR {target_room:.2f}",
        "Legs:",
        *leg_lines,
    ])


def build_market_close_message(
    account: str,
    spot: float | None,
    legs: list[Leg],
    net_pnl: float,
    action: str,
    close_time: str,
) -> str:
    leg_lines = [
        f"- {leg.symbol}: qty {leg.quantity}, LTP {leg.last_price:.2f}, P&L INR {leg.pnl:.2f}"
        for leg in legs
    ]
    if not leg_lines:
        leg_lines = ["- No open matching SENSEX legs visible."]
    return "\n".join([
        "SENSEX LIVE POSITION MONITOR STOPPED",
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}",
        f"Reason: market close cutoff {close_time}",
        f"Account: {account}",
        f"Spot: {format_money(spot)}",
        f"Final action: {action}",
        f"Final net P&L: INR {net_pnl:.2f}",
        "Final legs:",
        *leg_lines,
    ])


def render_report(
    account: str,
    spot: float | None,
    legs: list[Leg],
    net_pnl: float,
    action: str,
    message: str,
    max_loss: float,
    profit_target: float,
    telegram_status: dict[str, Any],
    heartbeat_status: dict[str, Any],
    lifecycle_status: dict[str, Any] | None = None,
) -> str:
    lines = [
        "# SENSEX Live Position Monitor",
        "",
        f"- Account: `{account}`",
        f"- Report Time: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}`",
        f"- SENSEX Spot: `{format_money(spot)}`",
        f"- Action: `{action}`",
        f"- Message: {message}",
        f"- Net P&L: `INR {net_pnl:.2f}`",
        f"- Max loss: `INR {max_loss:.2f}`",
        f"- Profit target: `INR {profit_target:.2f}`",
        f"- Telegram: `{telegram_status.get('telegram')}`",
        f"- Alerts sent: `{len(telegram_status.get('sent', []))}`",
        f"- Heartbeat: `{heartbeat_status.get('enabled')}` every `{heartbeat_status.get('interval_minutes')}` minutes",
        f"- Last heartbeat: `{heartbeat_status.get('last_sent_at') or '-'}`",
        f"- Lifecycle: `{(lifecycle_status or {}).get('status', 'RUNNING')}`",
        f"- Stop reason: `{(lifecycle_status or {}).get('reason', '-')}`",
        "",
        "## Legs",
        "| Symbol | Qty | Sell Avg | Buy Avg | LTP | P&L | Product |",
        "|---|---:|---:|---:|---:|---:|---|",
    ]
    if not legs:
        lines.append("| - | - | - | - | - | - | - |")
    for leg in legs:
        lines.append(
            f"| `{leg.symbol}` | `{leg.quantity}` | `{leg.sell_price:.2f}` | `{leg.buy_price:.2f}` | `{leg.last_price:.2f}` | `{leg.pnl:.2f}` | `{leg.product}` |"
        )
    if telegram_status.get("last_error"):
        lines.extend(["", f"- Last Telegram error: `{telegram_status['last_error']}`"])
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    env_text = Path(args.env_file).read_text()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "sensex_live_position_report.md"
    state_path = output_dir / "sensex_live_position_state.json"

    token = load_access_token(env_text, args.account)
    telegram_token, telegram_chat_id = telegram_credentials(env_text)
    telegram_enabled = bool(telegram_token and telegram_chat_id) and not args.disable_telegram_alerts
    sent_alerts = load_sent_alerts(state_path)
    loss_levels = parse_float_list(args.loss_alert_levels)
    profit_levels = parse_float_list(args.profit_alert_levels)
    client = UpstoxClient(token)
    last_error: str | None = None
    heartbeat_interval_seconds = max(0.0, args.heartbeat_minutes * 60.0)
    last_heartbeat_epoch = load_last_heartbeat_ts(state_path)
    last_heartbeat_at: str | None = None
    market_close_time = parse_hhmm(args.market_close_time)

    while True:
        loop_time = time.time()
        market_closed = is_market_closed(market_close_time)
        rows = client.get_positions()
        legs = open_sensex_legs(rows, args.exchange, args.symbol_contains)
        spot = client.sensex_spot()
        net_pnl = round(sum(leg.pnl for leg in legs), 2)
        if not legs:
            action, message = "NO_OPEN_POSITION", "No matching open SENSEX option position is currently visible."
        else:
            action, message = action_for_pnl(net_pnl, args.max_loss_rupees, args.profit_target_rupees)

        alert_keys, alert_message = build_alerts(action, net_pnl, loss_levels, profit_levels, sent_alerts)
        if alert_keys and alert_message:
            if telegram_enabled and telegram_token and telegram_chat_id:
                last_error = send_telegram_alert(telegram_token, telegram_chat_id, alert_message)
            else:
                last_error = "Telegram disabled or TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing."
            sent_alerts.update(alert_keys)

        heartbeat_sent = False
        heartbeat_error = None
        heartbeat_due = (
            heartbeat_interval_seconds > 0
            and (
                last_heartbeat_epoch is None
                or loop_time - last_heartbeat_epoch >= heartbeat_interval_seconds
            )
        )
        if heartbeat_due:
            heartbeat_message = build_heartbeat_message(
                args.account,
                spot,
                legs,
                net_pnl,
                action,
                message,
                args.max_loss_rupees,
                args.profit_target_rupees,
            )
            if telegram_enabled and telegram_token and telegram_chat_id:
                heartbeat_error = send_telegram_alert(telegram_token, telegram_chat_id, heartbeat_message)
            else:
                heartbeat_error = "Telegram disabled or TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing."
            if heartbeat_error:
                last_error = heartbeat_error
            else:
                heartbeat_sent = True
                last_heartbeat_epoch = loop_time
                last_heartbeat_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        telegram_status = {
            "telegram": "enabled" if telegram_enabled else "disabled_or_missing_env",
            "sent": sorted(sent_alerts),
            "last_error": last_error,
            "last_alert_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S") if alert_keys else None,
        }
        heartbeat_status = {
            "enabled": "yes" if heartbeat_interval_seconds > 0 and telegram_enabled else "disabled_or_missing_env",
            "interval_minutes": args.heartbeat_minutes,
            "last_sent_epoch": last_heartbeat_epoch,
            "last_sent_at": last_heartbeat_at,
            "sent_this_loop": heartbeat_sent,
            "last_error": heartbeat_error,
        }
        lifecycle_status = {
            "status": "STOPPED" if market_closed else "RUNNING",
            "reason": f"market close cutoff {args.market_close_time}" if market_closed else None,
            "market_close_time": args.market_close_time,
        }
        if market_closed and telegram_enabled and telegram_token and telegram_chat_id:
            market_close_key = f"market_close_{datetime.now().date().isoformat()}"
            if market_close_key not in sent_alerts:
                close_error = send_telegram_alert(
                    telegram_token,
                    telegram_chat_id,
                    build_market_close_message(args.account, spot, legs, net_pnl, action, args.market_close_time),
                )
                if close_error:
                    last_error = close_error
                    telegram_status["last_error"] = last_error
                sent_alerts.add(market_close_key)
                telegram_status["sent"] = sorted(sent_alerts)
        report = render_report(
            args.account,
            spot,
            legs,
            net_pnl,
            action,
            message,
            args.max_loss_rupees,
            args.profit_target_rupees,
            telegram_status,
            heartbeat_status,
            lifecycle_status,
        )
        report_path.write_text(report)
        write_json(state_path, {
            "account": args.account,
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "spot": spot,
            "action": action,
            "message": message,
            "net_pnl": net_pnl,
            "max_loss_rupees": args.max_loss_rupees,
            "profit_target_rupees": args.profit_target_rupees,
            "legs": [asdict(leg) for leg in legs],
            "alerts": telegram_status,
            "heartbeat": heartbeat_status,
            "lifecycle": lifecycle_status,
        })

        if args.once or market_closed:
            print(f"Wrote report to {report_path}")
            return 0
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
