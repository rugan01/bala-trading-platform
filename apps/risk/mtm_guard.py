#!/usr/bin/env python3.11
from __future__ import annotations

"""
MTM guard service for account-wide P&L monitoring and Telegram controls.

This service is intentionally conservative:
- broadcast MTM heartbeats to a Telegram alert chat/channel
- accept control commands only from whitelisted Telegram users/chats
- require explicit double confirmation before flattening positions
- auto-refresh stale Upstox tokens once on 401 responses

Typical usage:
    ./.venv/bin/python apps/risk/mtm_guard.py --account BALA --profit-target 5000 --loss-limit 3000
    ./.venv/bin/python apps/risk/mtm_guard.py --account BALA --once
"""

import argparse
import json
import logging
import os
import re
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import dotenv_values

REPO_ROOT = Path(__file__).resolve().parents[2]
TRADING_PLATFORM_SRC = REPO_ROOT / "packages" / "trading_platform" / "src"
if TRADING_PLATFORM_SRC.exists():
    import sys

    sys.path.insert(0, str(TRADING_PLATFORM_SRC))

try:
    from trading_platform.paths import ENV_FILE, RISK_RUNTIME_ROOT
except Exception:
    ENV_FILE = REPO_ROOT / ".env"
    RISK_RUNTIME_ROOT = REPO_ROOT / "data" / "runtime" / "risk"

try:
    from trading_platform.risk.daily_plan import (
        load_runtime_plan,
        symbol_matches_expiry_underlying,
        update_runtime_plan_state,
    )
except Exception:
    load_runtime_plan = None
    symbol_matches_expiry_underlying = None
    update_runtime_plan_state = None

SUPPORTED_ACCOUNTS = ("BALA", "NIMMY")
POSITIONS_URL = "https://api.upstox.com/v2/portfolio/short-term-positions"
EXIT_ALL_POSITIONS_URL = "https://api.upstox.com/v2/order/positions/exit"
FUNDS_MARGIN_V3_URL = "https://api.upstox.com/v3/user/get-funds-and-margin"
TELEGRAM_API_BASE = "https://api.telegram.org/bot{token}"
CONFIRMATION_TTL_SECONDS = 300
LOG_FILE = os.path.expanduser("~/Library/Logs/mtm_guard.log")

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PositionSnapshot:
    symbol: str
    exchange: str
    product: str
    quantity: int
    last_price: float
    pnl: float
    unrealised: float
    realised: float
    instrument_token: str


@dataclass(slots=True)
class MTMSnapshot:
    account: str
    captured_at: str
    net_pnl: float
    realised_pnl: float
    unrealised_pnl: float
    open_positions: int
    closed_positions_today: int
    positions: list[PositionSnapshot]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MTM guard with Telegram control hooks.")
    parser.add_argument("--account", choices=SUPPORTED_ACCOUNTS, default="BALA")
    parser.add_argument("--env-file", default=str(ENV_FILE))
    parser.add_argument("--state-dir", default=str(RISK_RUNTIME_ROOT))
    parser.add_argument("--heartbeat-seconds", type=int, default=300, help="Channel MTM update frequency. Default: 300.")
    parser.add_argument("--command-poll-seconds", type=int, default=5, help="Telegram command polling cadence. Default: 5.")
    parser.add_argument("--profit-target", type=float, default=None, help="Initial MTM profit target for the day.")
    parser.add_argument("--loss-limit", type=float, default=None, help="Initial MTM loss limit for the day.")
    parser.add_argument("--market-open-time", default="09:00")
    parser.add_argument("--market-close-time", default="23:30")
    parser.add_argument("--commodity-close-warning-minutes", type=int, default=30, help="Warn if commodity positions remain open this many minutes before close.")
    parser.add_argument("--dry-run-close", action="store_true", help="Do not call Upstox exit-all; just simulate it.")
    parser.add_argument("--disable-telegram-send", action="store_true", help="Log Telegram messages locally instead of sending them.")
    parser.add_argument(
        "--print-telegram-identities",
        action="store_true",
        help="Print recent Telegram update chat/user IDs for control bootstrap and exit.",
    )
    parser.add_argument("--once", action="store_true", help="Run one MTM cycle and exit.")
    return parser.parse_args()


def parse_hhmm(value: str) -> dtime:
    hour, minute = value.split(":")
    return dtime(hour=int(hour), minute=int(minute))


def now_local() -> datetime:
    return datetime.now().astimezone()


def today_local() -> date:
    return now_local().date()


def format_money(value: Optional[float]) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    return f"{sign}₹{value:,.2f}"


def format_plain_money(value: Optional[float]) -> str:
    if value is None:
        return "-"
    sign = "+" if value > 0 else ""
    return f"{sign}{value:,.2f}"


def env_values(path: Path) -> dict[str, str]:
    raw = dotenv_values(path)
    return {k: str(v).strip().strip('"').strip("'") for k, v in raw.items() if v is not None}


def env_value(values: dict[str, str], key: str) -> Optional[str]:
    value = values.get(key)
    if value is None:
        return None
    text = value.strip().strip('"').strip("'")
    return text or None


def load_access_token(values: dict[str, str], account: str) -> str:
    primary = env_value(values, f"UPSTOX_{account}_ACCESS_TOKEN")
    if primary:
        return primary
    if account == "BALA":
        fallback = env_value(values, "UPSTOX_ACCESS_TOKEN")
        if fallback:
            return fallback
    raise RuntimeError(f"Missing Upstox access token for {account}")


def load_telegram_token(values: dict[str, str]) -> str:
    token = env_value(values, "TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")
    return token


def load_alert_chat_id(values: dict[str, str]) -> Optional[str]:
    return env_value(values, "TELEGRAM_ALERT_CHAT_ID") or env_value(values, "TELEGRAM_CHAT_ID")


def parse_csv_ints(value: Optional[str]) -> set[int]:
    if not value:
        return set()
    result: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        result.add(int(part))
    return result


def is_within_hours(open_time: dtime, close_time: dtime, current: Optional[datetime] = None) -> bool:
    current_time = (current or now_local()).time()
    return open_time <= current_time <= close_time


class UpstoxRiskClient:
    def __init__(
        self,
        *,
        env_file: Path,
        account: str,
        auto_refresh_on_unauthorized: bool = True,
        dry_run_close: bool = False,
    ):
        self.env_file = env_file
        self.account = account.upper()
        self.auto_refresh_on_unauthorized = auto_refresh_on_unauthorized
        self.dry_run_close = dry_run_close
        self._refresh_attempted = False
        self._load_headers()

    def _load_headers(self) -> None:
        values = env_values(self.env_file)
        token = load_access_token(values, self.account)
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

    def _refresh_access_token(self) -> None:
        python_bin = REPO_ROOT / ".venv" / "bin" / "python"
        refresh_script = REPO_ROOT / "apps" / "journaling" / "upstox_token_refresh.py"
        if not python_bin.exists():
            raise RuntimeError(
                f"Automatic Upstox token refresh requires {python_bin} to exist. "
                "Create the repo .venv and install journaling dependencies first."
            )
        logger.info("[%s] Upstox token appears stale. Attempting automatic refresh...", self.account)
        result = subprocess.run(
            [
                str(python_bin),
                str(refresh_script),
                "--account",
                self.account,
                "--env-file",
                str(self.env_file),
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Automatic token refresh failed for {self.account}. {detail}")
        self._load_headers()
        logger.info("[%s] Automatic token refresh succeeded; retrying API call", self.account)

    def _request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict[str, Any]] = None,
        payload: Optional[dict[str, Any]] = None,
        extra_headers: Optional[dict[str, str]] = None,
        timeout: int = 30,
    ) -> requests.Response:
        headers = dict(self.headers)
        if extra_headers:
            headers.update(extra_headers)
        response = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            json=payload,
            timeout=timeout,
        )
        if (
            response.status_code == 401
            and self.auto_refresh_on_unauthorized
            and not self._refresh_attempted
        ):
            self._refresh_attempted = True
            self._refresh_access_token()
            headers = dict(self.headers)
            if extra_headers:
                headers.update(extra_headers)
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=payload,
                timeout=timeout,
            )
        response.raise_for_status()
        return response

    def get_positions(self) -> list[dict[str, Any]]:
        response = self._request("GET", POSITIONS_URL, timeout=20)
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"Upstox positions returned non-success payload: {payload}")
        return payload.get("data", [])

    def get_funds_and_margin(self) -> dict[str, Any]:
        response = self._request(
            "GET",
            FUNDS_MARGIN_V3_URL,
            extra_headers={"Api-Version": "3.0"},
            timeout=20,
        )
        payload = response.json()
        if payload.get("status") != "success":
            raise RuntimeError(f"Upstox funds/margin returned non-success payload: {payload}")
        return payload.get("data", {})

    def build_snapshot(self) -> MTMSnapshot:
        rows = self.get_positions()
        positions: list[PositionSnapshot] = []
        net_pnl = 0.0
        realised_pnl = 0.0
        unrealised_pnl = 0.0
        closed_positions_today = 0
        for row in rows:
            quantity = int(row.get("quantity") or 0)
            pnl = float(row.get("pnl") or 0.0)
            unrealised = float(row.get("unrealised") or 0.0)
            realised = float(row.get("realised") or 0.0)

            net_pnl += pnl
            realised_pnl += realised
            unrealised_pnl += unrealised

            if quantity == 0:
                if realised or pnl or int(row.get("day_buy_quantity") or 0) or int(row.get("day_sell_quantity") or 0):
                    closed_positions_today += 1
                continue

            positions.append(
                PositionSnapshot(
                    symbol=str(row.get("trading_symbol") or row.get("tradingsymbol") or ""),
                    exchange=str(row.get("exchange") or ""),
                    product=str(row.get("product") or ""),
                    quantity=quantity,
                    last_price=float(row.get("last_price") or 0.0),
                    pnl=pnl,
                    unrealised=unrealised,
                    realised=realised,
                    instrument_token=str(row.get("instrument_token") or ""),
                )
            )

        positions.sort(key=lambda item: abs(item.pnl), reverse=True)
        return MTMSnapshot(
            account=self.account,
            captured_at=now_local().isoformat(),
            net_pnl=round(net_pnl, 2),
            realised_pnl=round(realised_pnl, 2),
            unrealised_pnl=round(unrealised_pnl, 2),
            open_positions=len(positions),
            closed_positions_today=closed_positions_today,
            positions=positions,
        )

    def exit_all_positions(self) -> dict[str, Any]:
        if self.dry_run_close:
            logger.info("[%s] DRY RUN: would call Upstox exit-all positions", self.account)
            return {
                "status": "dry_run",
                "data": {"order_ids": []},
                "summary": {"total": 0, "success": 0, "error": 0},
            }

        response = self._request("POST", EXIT_ALL_POSITIONS_URL, payload={}, timeout=30)
        payload = response.json()
        status = payload.get("status")
        if status not in {"success", "partial_success"}:
            raise RuntimeError(f"Upstox exit-all failed: {payload}")
        return payload


class TelegramClient:
    def __init__(self, token: str, *, disable_send: bool = False):
        self.token = token
        self.base_url = TELEGRAM_API_BASE.format(token=token)
        self.disable_send = disable_send

    def _post(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self.disable_send and method != "getUpdates":
            logger.info("[Telegram DRY] %s %s", method, payload)
            return {"ok": True, "result": {"message_id": 0}}
        response = requests.post(f"{self.base_url}/{method}", json=payload, timeout=20)
        if not response.ok:
            detail: str
            try:
                data = response.json()
                detail = json.dumps(data, ensure_ascii=False)
            except ValueError:
                detail = response.text.strip() or response.reason
            raise RuntimeError(
                f"Telegram API {method} failed with HTTP {response.status_code}: {detail}"
            )
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API {method} failed: {data}")
        return data

    def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: str = "Markdown",
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
            "parse_mode": parse_mode,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            return self._post("sendMessage", payload)
        except RuntimeError as exc:
            if parse_mode and "can't parse entities" in str(exc).lower():
                fallback_payload = dict(payload)
                fallback_payload.pop("parse_mode", None)
                logger.warning(
                    "Telegram Markdown send failed; retrying without parse_mode. Error: %s",
                    exc,
                )
                return self._post("sendMessage", fallback_payload)
            raise

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> None:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        self._post("answerCallbackQuery", payload)

    def edit_message_reply_markup(self, chat_id: int | str, message_id: int, reply_markup: Optional[dict[str, Any]] = None) -> None:
        payload: dict[str, Any] = {"chat_id": chat_id, "message_id": message_id}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self._post("editMessageReplyMarkup", payload)

    def get_updates(self, *, offset: int = 0, timeout: int = 0) -> list[dict[str, Any]]:
        payload = {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        result = self._post("getUpdates", payload)
        return result.get("result", [])


def default_state(account: str, session_date: date) -> dict[str, Any]:
    return {
        "account": account,
        "session_date": session_date.isoformat(),
        "profit_target": None,
        "loss_limit": None,
        "paused": False,
        "telegram_offset": 0,
        "last_heartbeat_at": None,
        "pending_confirmations": {},
        "threshold_alerts_sent": [],
        "frozen_for_new_trades": False,
        "freeze_reason": None,
        "hard_stop_reached": False,
    }


class StateStore:
    def __init__(self, state_dir: Path, account: str):
        self.state_dir = state_dir
        self.account = account
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / f"mtm_guard_{account.lower()}.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return default_state(self.account, today_local())
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            logger.warning("State file was invalid JSON. Resetting: %s", self.path)
            return default_state(self.account, today_local())

        session_date = data.get("session_date")
        today_text = today_local().isoformat()
        if session_date != today_text:
            telegram_offset = int(data.get("telegram_offset") or 0)
            refreshed = default_state(self.account, today_local())
            refreshed["telegram_offset"] = telegram_offset
            return refreshed
        return data

    def save(self, state: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(state, indent=2, default=str))


def summarize_positions(snapshot: MTMSnapshot, *, limit: int = 4) -> str:
    if not snapshot.positions:
        return "No open positions."
    lines = []
    for position in snapshot.positions[:limit]:
        lines.append(
            f"- `{position.symbol}` | qty `{position.quantity}` | LTP `{position.last_price:.2f}` | "
            f"P&L `{format_plain_money(position.pnl)}`"
        )
    remaining = snapshot.open_positions - min(limit, len(snapshot.positions))
    if remaining > 0:
        lines.append(f"- ... plus `{remaining}` more open position(s)")
    return "\n".join(lines)


def build_status_message(state: dict[str, Any], snapshot: MTMSnapshot, *, source: str) -> str:
    threshold_line = (
        f"Target `{format_money(state.get('profit_target'))}` | "
        f"Loss `{format_money(-abs(state['loss_limit'])) if state.get('loss_limit') is not None else '-'}`"
    )
    pause_text = "paused" if state.get("paused") else "running"
    freeze_text = "frozen" if state.get("frozen_for_new_trades") else "not frozen"
    return "\n".join(
        [
            f"*{snapshot.account} MTM Guard*",
            f"Source: `{source}`",
            f"Time: `{datetime.fromisoformat(snapshot.captured_at).strftime('%Y-%m-%d %H:%M:%S IST')}`",
            f"Service: `{pause_text}`",
            f"New-trade state: `{freeze_text}`",
            f"Freeze reason: `{state.get('freeze_reason') or '-'}`",
            "",
            f"Net MTM: `{format_money(snapshot.net_pnl)}`",
            f"Realised: `{format_money(snapshot.realised_pnl)}`",
            f"Unrealised: `{format_money(snapshot.unrealised_pnl)}`",
            f"Open positions: `{snapshot.open_positions}`",
            f"Closed today: `{snapshot.closed_positions_today}`",
            f"Daily limits: {threshold_line}",
            "",
            "Positions:",
            summarize_positions(snapshot),
        ]
    )


def build_confirmation_message(action_label: str, detail_lines: list[str]) -> str:
    return "\n".join(
        [
            f"*Confirm {action_label}*",
            *detail_lines,
            "",
            "Please confirm or cancel.",
        ]
    )


def build_service_event_message(
    account: str,
    *,
    state: dict[str, Any],
    event: str,
    detail: Optional[str] = None,
) -> str:
    lines = [
        f"*{account} MTM Guard*",
        f"Event: `{event}`",
        f"Time: `{now_local().strftime('%Y-%m-%d %H:%M:%S IST')}`",
        f"Service: `{'paused' if state.get('paused') else 'running'}`",
        f"New-trade state: `{'frozen' if state.get('frozen_for_new_trades') else 'not frozen'}`",
        f"Profit target: `{format_money(state.get('profit_target'))}`",
        f"Loss limit: `{format_money(-abs(state['loss_limit'])) if state.get('loss_limit') is not None else '-'}`",
    ]
    if detail:
        lines.extend(["", detail])
    return "\n".join(lines)


def summarize_recent_telegram_identities(updates: list[dict[str, Any]]) -> str:
    identities: dict[tuple[int, Optional[int]], dict[str, Any]] = {}
    for update in updates:
        payload = update.get("message") or update.get("callback_query") or {}
        if "message" not in update and "callback_query" in update:
            payload = update["callback_query"]
            chat = (payload.get("message") or {}).get("chat") or {}
            from_user = payload.get("from") or {}
        else:
            chat = payload.get("chat") or {}
            from_user = payload.get("from") or {}

        chat_id = chat.get("id")
        if chat_id is None:
            continue
        user_id = from_user.get("id")
        key = (int(chat_id), int(user_id) if user_id is not None else None)
        identities[key] = {
            "chat_id": int(chat_id),
            "chat_type": chat.get("type") or "?",
            "chat_title": chat.get("title") or chat.get("username") or chat.get("first_name") or "",
            "user_id": int(user_id) if user_id is not None else None,
            "user_name": from_user.get("username") or from_user.get("first_name") or "",
        }

    if not identities:
        return "No recent Telegram updates found. Send the bot a private `/status` or `/start`, then rerun this command."

    lines = ["Recent Telegram identities:"]
    for item in identities.values():
        user_part = str(item["user_id"]) if item["user_id"] is not None else "-"
        lines.append(
            f"- chat_id={item['chat_id']} chat_type={item['chat_type']} "
            f"chat={item['chat_title'] or '-'} user_id={user_part} user={item['user_name'] or '-'}"
        )
    return "\n".join(lines)


def make_confirm_markup(token: str) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "Confirm", "callback_data": f"mtm:confirm:{token}"},
                {"text": "Cancel", "callback_data": f"mtm:cancel:{token}"},
            ]
        ]
    }


def make_status_markup(account: str, paused: bool) -> dict[str, Any]:
    pause_action = "resume" if paused else "pause"
    pause_label = "Resume" if paused else "Pause"
    return {
        "inline_keyboard": [
            [
                {"text": "Refresh", "callback_data": f"mtm:status:{account}"},
                {"text": pause_label, "callback_data": f"mtm:{pause_action}:{account}"},
                {"text": "Close All", "callback_data": f"mtm:close:{account}"},
            ]
        ]
    }


def parse_plan_hhmm(value: Optional[str]) -> Optional[dtime]:
    if not value:
        return None
    try:
        return parse_hhmm(str(value))
    except Exception:
        return None


def minutes_until_time(target: dtime, current: Optional[datetime] = None) -> float:
    current_dt = current or now_local()
    target_dt = current_dt.replace(hour=target.hour, minute=target.minute, second=0, microsecond=0)
    return (target_dt - current_dt).total_seconds() / 60


def is_commodity_position(position: PositionSnapshot) -> bool:
    exchange = position.exchange.upper()
    symbol = position.symbol.upper()
    return "MCX" in exchange or any(symbol.startswith(root) for root in ("SILVER", "GOLD", "CRUDE", "NATGAS", "ZINC"))


def positions_net_pnl(positions: list[PositionSnapshot]) -> float:
    return sum(float(position.pnl) for position in positions)


def user_and_chat_allowed(
    *,
    chat_id: int,
    user_id: Optional[int],
    allowed_user_ids: set[int],
    allowed_chat_ids: set[int],
) -> bool:
    if not allowed_user_ids:
        return False
    if allowed_user_ids and user_id not in allowed_user_ids:
        return False
    if allowed_chat_ids and chat_id not in allowed_chat_ids:
        return False
    return True


def pending_confirmation_expired(
    pending: dict[str, Any],
    *,
    current: Optional[datetime] = None,
    ttl_seconds: int = CONFIRMATION_TTL_SECONDS,
) -> bool:
    created_at = pending.get("created_at")
    if not created_at:
        return True
    try:
        created = datetime.fromisoformat(str(created_at))
    except (TypeError, ValueError):
        return True
    now = current or now_local()
    if created.tzinfo is None and now.tzinfo is not None:
        created = created.replace(tzinfo=now.tzinfo)
    return (now - created).total_seconds() > ttl_seconds


def parse_account_token(token: Optional[str], default_account: str) -> str:
    if not token:
        return default_account
    upper = token.upper()
    if upper in SUPPORTED_ACCOUNTS:
        return upper
    return default_account


def parse_command(text: str, default_account: str) -> tuple[str, dict[str, Any]]:
    tokens = text.strip().split()
    if not tokens:
        raise ValueError("Empty command")
    command = tokens[0].split("@", 1)[0].lower()

    if command in {"/help", "/start"}:
        return "help", {"account": default_account}

    if command in {"/status", "/refresh", "/close", "/pause", "/resume", "/stop"}:
        account = parse_account_token(tokens[1], default_account) if len(tokens) > 1 else default_account
        return command.lstrip("/"), {"account": account}

    if command == "/set":
        idx = 1
        account = default_account
        if idx < len(tokens) and tokens[idx].upper() in SUPPORTED_ACCOUNTS:
            account = tokens[idx].upper()
            idx += 1
        if idx >= len(tokens):
            raise ValueError("Usage: /set [BALA|NIMMY] limits <profit> <loss> | /set profit <value> | /set loss <value>")
        mode = tokens[idx].lower()
        idx += 1
        if mode == "limits":
            if len(tokens) - idx < 2:
                raise ValueError("Usage: /set limits <profit_target> <loss_limit>")
            return "set_limits", {
                "account": account,
                "profit_target": float(tokens[idx]),
                "loss_limit": abs(float(tokens[idx + 1])),
            }
        if mode == "profit":
            if len(tokens) - idx < 1:
                raise ValueError("Usage: /set profit <value>")
            return "set_profit", {"account": account, "profit_target": float(tokens[idx])}
        if mode == "loss":
            if len(tokens) - idx < 1:
                raise ValueError("Usage: /set loss <value>")
            return "set_loss", {"account": account, "loss_limit": abs(float(tokens[idx]))}
        raise ValueError("Unknown /set mode. Use profit, loss, or limits.")

    raise ValueError("Unknown command. Use /help for available commands.")


class MTMGuardService:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.account = args.account.upper()
        self.env_file = Path(args.env_file)
        self.state_store = StateStore(Path(args.state_dir), self.account)
        self.open_time = parse_hhmm(args.market_open_time)
        self.close_time = parse_hhmm(args.market_close_time)
        self.state = self.state_store.load()
        if args.profit_target is not None:
            self.state["profit_target"] = float(args.profit_target)
        if args.loss_limit is not None:
            self.state["loss_limit"] = abs(float(args.loss_limit))
        self.state_store.save(self.state)
        self.shutdown_requested = False

        values = env_values(self.env_file)
        self.telegram = TelegramClient(
            load_telegram_token(values),
            disable_send=args.disable_telegram_send,
        )
        self.alert_chat_id = load_alert_chat_id(values)
        self.allowed_user_ids = parse_csv_ints(env_value(values, "TELEGRAM_ALLOWED_USER_IDS"))
        self.allowed_chat_ids = parse_csv_ints(env_value(values, "TELEGRAM_CONTROL_CHAT_IDS"))
        self.control_chat_fallback = next(iter(self.allowed_chat_ids), None)
        self.upstox = UpstoxRiskClient(
            env_file=self.env_file,
            account=self.account,
            dry_run_close=args.dry_run_close,
        )
        self._sync_plan_defaults()
        if not self.allowed_user_ids:
            logger.warning(
                "[%s] Telegram control commands are disabled until TELEGRAM_ALLOWED_USER_IDS is configured.",
                self.account,
            )

    def print_recent_telegram_identities(self) -> None:
        updates = self.telegram.get_updates(offset=0, timeout=0)
        print(summarize_recent_telegram_identities(updates))

    def _refresh_telegram_config(self) -> None:
        values = env_values(self.env_file)
        self.alert_chat_id = load_alert_chat_id(values)
        self.allowed_user_ids = parse_csv_ints(env_value(values, "TELEGRAM_ALLOWED_USER_IDS"))
        self.allowed_chat_ids = parse_csv_ints(env_value(values, "TELEGRAM_CONTROL_CHAT_IDS"))
        if self.allowed_chat_ids:
            self.control_chat_fallback = next(iter(self.allowed_chat_ids), None)

    def _load_plan_context(self) -> dict[str, Any]:
        if load_runtime_plan is None:
            return {}
        try:
            return load_runtime_plan(self.account, runtime_root=Path(self.args.state_dir), plan_date=today_local())
        except Exception as exc:
            logger.warning("[%s] Failed to load runtime daily plan: %s", self.account, exc)
            return {}

    def _sync_plan_defaults(self) -> None:
        plan_context = self._load_plan_context()
        if not plan_context:
            return
        changed = False
        if self.state.get("loss_limit") is None and plan_context.get("hard_daily_stop") is not None:
            self.state["loss_limit"] = abs(float(plan_context["hard_daily_stop"]))
            self.state["loss_limit_source"] = "runtime_daily_plan"
            changed = True
        if changed:
            self.state_store.save(self.state)

    def _expiry_positions(self, snapshot: MTMSnapshot, plan_context: dict[str, Any]) -> list[PositionSnapshot]:
        expiry_pilot = plan_context.get("expiry_pilot") or {}
        underlying = expiry_pilot.get("underlying")
        if not expiry_pilot.get("enabled") or not underlying or symbol_matches_expiry_underlying is None:
            return []
        return [
            position
            for position in snapshot.positions
            if symbol_matches_expiry_underlying(position.symbol, str(underlying))
        ]

    def _freeze_for_new_trades(self, reason: str) -> None:
        self.state["frozen_for_new_trades"] = True
        self.state["freeze_reason"] = reason
        self.state["hard_stop_reached"] = True
        self.state_store.save(self.state)
        if update_runtime_plan_state is None:
            return
        try:
            update_runtime_plan_state(
                self.account,
                {
                    "frozen_for_new_trades": True,
                    "freeze_reason": reason,
                    "hard_stop_reached": True,
                },
                runtime_root=Path(self.args.state_dir),
                plan_date=today_local(),
            )
        except Exception as exc:
            logger.warning("[%s] Failed to update runtime plan freeze state: %s", self.account, exc)

    def _send_help(self, chat_id: int) -> None:
        message = "\n".join(
            [
                "*MTM Guard Commands*",
                f"Current service account: `{self.account}`",
                "",
                "`/status` or `/status BALA`",
                "`/set limits 5000 3000`",
                "`/set profit 5000`",
                "`/set loss 3000`",
                "`/pause`",
                "`/resume`",
                "`/stop`",
                "`/close`",
                "",
                "State-changing commands require confirmation.",
                "`/stop` terminates this running Python process.",
            ]
        )
        self.telegram.send_message(chat_id, message)

    def _persist_pending(self, action: str, actor_user_id: int, chat_id: int, payload: dict[str, Any]) -> str:
        token = uuid.uuid4().hex[:10]
        pending = self.state.setdefault("pending_confirmations", {})
        pending[token] = {
            "action": action,
            "payload": payload,
            "user_id": actor_user_id,
            "chat_id": chat_id,
            "created_at": now_local().isoformat(),
        }
        self.state_store.save(self.state)
        return token

    def _send_confirmation(self, chat_id: int, action: str, payload: dict[str, Any], actor_user_id: int) -> None:
        if action == "close":
            lines = [
                f"Account: `{payload['account']}`",
                "This will send Upstox *Exit All Positions* for the account.",
                "Use this only if you truly want to flatten everything.",
            ]
            label = "Close All Positions"
        elif action == "pause":
            lines = [f"Account: `{payload['account']}`", "Pause MTM heartbeats and threshold alerts."]
            label = "Pause MTM Guard"
        elif action == "resume":
            lines = [f"Account: `{payload['account']}`", "Resume MTM heartbeats and threshold alerts."]
            label = "Resume MTM Guard"
        elif action == "stop":
            lines = [
                f"Account: `{payload['account']}`",
                "This will terminate the running MTM guard process.",
                "Use `/pause` if you only want to stop alerts and control actions while keeping the bot alive.",
            ]
            label = "Stop MTM Guard Process"
        elif action == "set_limits":
            lines = [
                f"Account: `{payload['account']}`",
                f"Profit target: `{format_money(payload['profit_target'])}`",
                f"Loss limit: `-₹{payload['loss_limit']:,.2f}`",
            ]
            label = "Set Daily Limits"
        elif action == "set_profit":
            lines = [
                f"Account: `{payload['account']}`",
                f"Profit target: `{format_money(payload['profit_target'])}`",
            ]
            label = "Set Profit Target"
        elif action == "set_loss":
            lines = [
                f"Account: `{payload['account']}`",
                f"Loss limit: `-₹{payload['loss_limit']:,.2f}`",
            ]
            label = "Set Loss Limit"
        else:
            raise ValueError(f"Unsupported confirmable action: {action}")

        token = self._persist_pending(action, actor_user_id, chat_id, payload)
        self.telegram.send_message(
            chat_id,
            build_confirmation_message(label, lines),
            reply_markup=make_confirm_markup(token),
        )

    def _handle_status(self, chat_id: int, source: str = "command") -> None:
        snapshot = self.upstox.build_snapshot()
        self.telegram.send_message(
            chat_id,
            build_status_message(self.state, snapshot, source=source),
            reply_markup=make_status_markup(self.account, bool(self.state.get("paused"))),
        )

    def _execute_confirmed_action(self, action: str, payload: dict[str, Any]) -> str:
        account = payload.get("account", self.account).upper()
        if account != self.account:
            raise RuntimeError(f"This MTM guard instance manages {self.account}, not {account}.")

        if action == "close":
            result = self.upstox.exit_all_positions()
            order_ids = ((result.get("data") or {}).get("order_ids") or [])
            summary = result.get("summary") or {}
            return (
                f"Close-all request sent for `{account}`.\n"
                f"Status: `{result.get('status')}` | Orders: `{len(order_ids)}` | "
                f"Success: `{summary.get('success', 0)}` | Error: `{summary.get('error', 0)}`"
            )

        if action == "pause":
            self.state["paused"] = True
            self.state_store.save(self.state)
            return f"MTM guard paused for `{account}`."

        if action == "resume":
            self.state["paused"] = False
            self.state_store.save(self.state)
            return f"MTM guard resumed for `{account}`."

        if action == "stop":
            self.shutdown_requested = True
            return (
                f"MTM guard stop requested for `{account}`.\n"
                "This process will terminate after the current Telegram action completes."
            )

        if action == "set_limits":
            self.state["profit_target"] = float(payload["profit_target"])
            self.state["loss_limit"] = abs(float(payload["loss_limit"]))
            self.state["threshold_alerts_sent"] = []
            self.state_store.save(self.state)
            return (
                f"Updated `{account}` daily limits.\n"
                f"Profit target: `{format_money(self.state['profit_target'])}`\n"
                f"Loss limit: `-₹{self.state['loss_limit']:,.2f}`"
            )

        if action == "set_profit":
            self.state["profit_target"] = float(payload["profit_target"])
            self.state["threshold_alerts_sent"] = []
            self.state_store.save(self.state)
            return f"Updated `{account}` profit target to `{format_money(self.state['profit_target'])}`."

        if action == "set_loss":
            self.state["loss_limit"] = abs(float(payload["loss_limit"]))
            self.state["threshold_alerts_sent"] = []
            self.state_store.save(self.state)
            return f"Updated `{account}` loss limit to `-₹{self.state['loss_limit']:,.2f}`."

        raise RuntimeError(f"Unsupported confirmed action: {action}")

    def _handle_callback(self, callback_query: dict[str, Any]) -> None:
        callback_id = callback_query["id"]
        data = callback_query.get("data", "")
        message = callback_query.get("message") or {}
        chat = message.get("chat") or {}
        from_user = callback_query.get("from") or {}
        chat_id = int(chat.get("id"))
        user_id = int(from_user.get("id"))

        if not user_and_chat_allowed(
            chat_id=chat_id,
            user_id=user_id,
            allowed_user_ids=self.allowed_user_ids,
            allowed_chat_ids=self.allowed_chat_ids,
        ):
            self.telegram.answer_callback_query(callback_id, "Not authorized.")
            return

        if data.startswith("mtm:status:"):
            self.telegram.answer_callback_query(callback_id, "Refreshing status...")
            self._handle_status(chat_id, source="button")
            return

        if data.startswith("mtm:close:"):
            self.telegram.answer_callback_query(callback_id, "Preparing confirmation...")
            self._send_confirmation(chat_id, "close", {"account": self.account}, user_id)
            return

        if data.startswith("mtm:pause:"):
            self.telegram.answer_callback_query(callback_id, "Preparing confirmation...")
            self._send_confirmation(chat_id, "pause", {"account": self.account}, user_id)
            return

        if data.startswith("mtm:resume:"):
            self.telegram.answer_callback_query(callback_id, "Preparing confirmation...")
            self._send_confirmation(chat_id, "resume", {"account": self.account}, user_id)
            return

        if data.startswith("mtm:confirm:") or data.startswith("mtm:cancel:"):
            mode, token = data.split(":")[1], data.split(":")[2]
            pending = self.state.get("pending_confirmations", {}).get(token)
            if not pending:
                self.telegram.answer_callback_query(callback_id, "This confirmation is no longer active.")
                return
            if pending_confirmation_expired(pending):
                self.state["pending_confirmations"].pop(token, None)
                self.state_store.save(self.state)
                self.telegram.edit_message_reply_markup(
                    chat_id,
                    int(message["message_id"]),
                    reply_markup={"inline_keyboard": []},
                )
                self.telegram.answer_callback_query(
                    callback_id,
                    "This confirmation expired. Start the request again.",
                )
                return
            if int(pending.get("user_id")) != user_id:
                self.telegram.answer_callback_query(callback_id, "Only the requesting user can confirm this action.")
                return
            if int(pending.get("chat_id")) != chat_id:
                self.telegram.answer_callback_query(
                    callback_id,
                    "Confirm this action in the chat where it was requested.",
                )
                return

            self.telegram.edit_message_reply_markup(chat_id, int(message["message_id"]), reply_markup={"inline_keyboard": []})
            if mode == "cancel":
                self.state["pending_confirmations"].pop(token, None)
                self.state_store.save(self.state)
                self.telegram.answer_callback_query(callback_id, "Cancelled.")
                self.telegram.send_message(chat_id, "*Request cancelled.*")
                return

            self.telegram.answer_callback_query(callback_id, "Executing...")
            result_text = self._execute_confirmed_action(pending["action"], pending["payload"])
            self.state["pending_confirmations"].pop(token, None)
            self.state_store.save(self.state)
            self.telegram.send_message(chat_id, result_text)
            if pending["action"] == "close" and self.alert_chat_id and str(self.alert_chat_id) != str(chat_id):
                self.telegram.send_message(self.alert_chat_id, f"*{self.account} MTM Guard*\n{result_text}")
            return

        self.telegram.answer_callback_query(callback_id, "Unknown action.")

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        from_user = message.get("from") or {}
        chat_id = int(chat.get("id"))
        user_id = int(from_user.get("id"))
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return

        if not user_and_chat_allowed(
            chat_id=chat_id,
            user_id=user_id,
            allowed_user_ids=self.allowed_user_ids,
            allowed_chat_ids=self.allowed_chat_ids,
        ):
            self.telegram.send_message(chat_id, "You are not authorized to control this MTM guard.")
            return

        try:
            action, payload = parse_command(text, self.account)
        except Exception as exc:
            self.telegram.send_message(chat_id, f"Command error: {exc}")
            return

        account = payload.get("account", self.account).upper()
        if account != self.account:
            self.telegram.send_message(chat_id, f"This MTM guard instance manages `{self.account}`, not `{account}`.")
            return

        if action == "help":
            self._send_help(chat_id)
            return
        if action in {"status", "refresh"}:
            self._handle_status(chat_id, source="command")
            return
        if action in {"close", "pause", "resume", "stop", "set_limits", "set_profit", "set_loss"}:
            self._send_confirmation(chat_id, action, payload, user_id)
            return
        self.telegram.send_message(chat_id, f"Unhandled action: {action}")

    def process_telegram_updates(self) -> None:
        self._refresh_telegram_config()
        offset = int(self.state.get("telegram_offset") or 0)
        updates = self.telegram.get_updates(offset=offset, timeout=0)
        if not updates:
            return
        for update in updates:
            update_id = int(update["update_id"])
            self.state["telegram_offset"] = update_id + 1
            if "callback_query" in update:
                self._handle_callback(update["callback_query"])
            elif "message" in update:
                self._handle_message(update["message"])
        self.state_store.save(self.state)

    def _send_heartbeat(self, snapshot: MTMSnapshot) -> None:
        if not self.alert_chat_id:
            logger.info("Skipping heartbeat because TELEGRAM_ALERT_CHAT_ID / TELEGRAM_CHAT_ID is not configured.")
            return
        message = build_status_message(self.state, snapshot, source="heartbeat")
        self.telegram.send_message(self.alert_chat_id, message)
        self.state["last_heartbeat_at"] = now_local().isoformat()
        self.state_store.save(self.state)

    def _send_startup_notice(self) -> None:
        if not self.alert_chat_id:
            return
        detail = "Telegram control is enabled." if self.allowed_user_ids else "Telegram control is disabled until allowed user IDs are configured."
        self.telegram.send_message(
            self.alert_chat_id,
            build_service_event_message(
                self.account,
                state=self.state,
                event="service_started",
                detail=detail,
            ),
        )

    def _evaluate_thresholds(self, snapshot: MTMSnapshot) -> None:
        sent = set(self.state.get("threshold_alerts_sent", []))
        new_keys: list[str] = []
        messages: list[str] = []
        plan_context = self._load_plan_context()
        expiry_pilot = plan_context.get("expiry_pilot") or {}
        expiry_positions = self._expiry_positions(snapshot, plan_context)

        loss_limit = self.state.get("loss_limit")
        if loss_limit is not None and snapshot.net_pnl <= -abs(float(loss_limit)):
            key = "loss_limit_hit"
            if key not in sent:
                new_keys.append(key)
                messages.append(
                    f"*{self.account} MTM loss limit reached*\n"
                    f"Net MTM: `{format_money(snapshot.net_pnl)}`\n"
                    f"Configured loss limit: `-₹{abs(float(loss_limit)):,.2f}`"
                )

        if expiry_pilot.get("enabled") and expiry_positions:
            underlying = expiry_pilot.get("underlying") or "EXPIRY"
            session_stop = abs(float(expiry_pilot.get("session_hard_stop") or 0))
            expiry_net_pnl = positions_net_pnl(expiry_positions)
            if session_stop and expiry_net_pnl <= -session_stop:
                key = "expiry_pilot_hard_stop_hit"
                if key not in sent:
                    new_keys.append(key)
                    self._freeze_for_new_trades("expiry_pilot_hard_stop")
                    messages.append(
                        "\n".join(
                            [
                                f"*{self.account} EXPIRY PILOT HARD STOP*",
                                f"Underlying: `{underlying}`",
                                f"Expiry pilot MTM: `{format_money(expiry_net_pnl)}`",
                                f"Account net MTM: `{format_money(snapshot.net_pnl)}`",
                                f"Pilot session stop: `-₹{session_stop:,.2f}`",
                                "",
                                "Action required: use *Close All* confirmation now.",
                                "New trades are frozen for this account for the rest of the session.",
                                "",
                                "Positions:",
                                summarize_positions(snapshot),
                            ]
                        )
                    )

            hard_close_time = parse_plan_hhmm(expiry_pilot.get("hard_close_time"))
            if hard_close_time and now_local().time() >= hard_close_time:
                key = "expiry_hard_close_time"
                if key not in sent:
                    new_keys.append(key)
                    messages.append(
                        "\n".join(
                            [
                                f"*{self.account} Expiry Hard-Close Time*",
                                f"Underlying: `{underlying}`",
                                f"Hard close time: `{expiry_pilot.get('hard_close_time')}`",
                                "",
                                "Expiry positions are still open. Close them now; no gamma gambling after hard-close time.",
                                "",
                                "Positions:",
                                summarize_positions(snapshot),
                            ]
                        )
                    )

        commodity_positions = [position for position in snapshot.positions if is_commodity_position(position)]
        if commodity_positions:
            minutes_to_close = minutes_until_time(self.close_time)
            if minutes_to_close <= float(self.args.commodity_close_warning_minutes):
                key = "commodity_near_close_open_positions"
                if key not in sent:
                    new_keys.append(key)
                    self._freeze_for_new_trades("commodity_near_close_open_positions")
                    position_lines = "\n".join(
                        f"- `{position.symbol}` | qty `{position.quantity}` | P&L `{format_plain_money(position.pnl)}`"
                        for position in commodity_positions[:6]
                    )
                    messages.append(
                        "\n".join(
                            [
                                f"*{self.account} Commodity Near-Close Alert*",
                                f"Minutes to configured close: `{minutes_to_close:.1f}`",
                                "Rule: no unplanned overnight commodity futures.",
                                "",
                                "Action required: close or explicitly hedge/reduce. New trades are frozen.",
                                "",
                                "Commodity positions:",
                                position_lines,
                            ]
                        )
                    )

        profit_target = self.state.get("profit_target")
        if profit_target is not None and snapshot.net_pnl >= float(profit_target):
            key = "profit_target_hit"
            if key not in sent:
                new_keys.append(key)
                messages.append(
                    f"*{self.account} MTM profit target reached*\n"
                    f"Net MTM: `{format_money(snapshot.net_pnl)}`\n"
                    f"Configured profit target: `{format_money(float(profit_target))}`"
                )

        if not messages:
            return

        control_chat = self.control_chat_fallback or self.alert_chat_id
        if control_chat:
            for message in messages:
                self.telegram.send_message(
                    control_chat,
                    message,
                    reply_markup=make_status_markup(self.account, bool(self.state.get("paused"))),
                )
        sent.update(new_keys)
        self.state["threshold_alerts_sent"] = sorted(sent)
        self.state_store.save(self.state)

    def run_once(self) -> MTMSnapshot:
        snapshot = self.upstox.build_snapshot()
        self._evaluate_thresholds(snapshot)
        self._send_heartbeat(snapshot)
        return snapshot

    def run_forever(self) -> None:
        logger.info("[%s] MTM guard starting. Profit target=%s Loss limit=%s", self.account, self.state.get("profit_target"), self.state.get("loss_limit"))
        self._send_startup_notice()
        while not self.shutdown_requested:
            try:
                self.process_telegram_updates()
                should_heartbeat = False
                if is_within_hours(self.open_time, self.close_time):
                    last_heartbeat = self.state.get("last_heartbeat_at")
                    if not last_heartbeat:
                        should_heartbeat = True
                    else:
                        elapsed = (now_local() - datetime.fromisoformat(last_heartbeat)).total_seconds()
                        should_heartbeat = elapsed >= self.args.heartbeat_seconds
                if should_heartbeat and not self.state.get("paused"):
                    snapshot = self.upstox.build_snapshot()
                    self._evaluate_thresholds(snapshot)
                    self._send_heartbeat(snapshot)
            except Exception as exc:
                logger.exception("[%s] MTM guard loop failed: %s", self.account, exc)
                if self.alert_chat_id:
                    try:
                        self.telegram.send_message(
                            self.alert_chat_id,
                            f"*{self.account} MTM Guard Error*\n`{str(exc)[:300]}`",
                        )
                    except Exception:
                        logger.exception("Failed to send Telegram error notification")
            if not self.shutdown_requested:
                time.sleep(max(1, int(self.args.command_poll_seconds)))
        logger.info("[%s] MTM guard stopped by Telegram/operator request.", self.account)


def main() -> None:
    args = parse_args()
    service = MTMGuardService(args)

    if args.print_telegram_identities:
        service.print_recent_telegram_identities()
        return

    if args.once:
        snapshot = service.upstox.build_snapshot()
        service._evaluate_thresholds(snapshot)
        print(build_status_message(service.state, snapshot, source="once"))
        return

    service.run_forever()


if __name__ == "__main__":
    main()
