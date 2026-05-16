#!/usr/bin/env python3.11
from __future__ import annotations

"""
Trading-day watch loop for live coaching support.

This service reuses the existing morning brief and live-analysis pipeline, then
adds BALA MTM / open-position awareness on top. It is intentionally lightweight:

- refreshes the live analysis every cycle
- reads thesis status for indices + top bullish/bearish stocks
- snapshots BALA positions / MTM
- prints a concise terminal dashboard
- sends Telegram alerts only when risk or thesis meaningfully changes
"""

import argparse
import json
import logging
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Optional

from mtm_guard import (
    ENV_FILE,
    REPO_ROOT,
    RISK_RUNTIME_ROOT,
    MTMSnapshot,
    TelegramClient,
    UpstoxRiskClient,
    env_values,
    format_money,
    is_within_hours,
    load_alert_chat_id,
    load_telegram_token,
    now_local,
    parse_hhmm,
    summarize_positions,
    today_local,
)

LOG_FILE = os.path.expanduser("~/Library/Logs/trading_day_watch.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(), logging.FileHandler(LOG_FILE)],
)
logger = logging.getLogger(__name__)

MORNING_BRIEF_SCRIPT = REPO_ROOT / "apps" / "briefing" / "morning_brief.py"
LIVE_ANALYSIS_SCRIPT = REPO_ROOT / "apps" / "briefing" / "live_analysis.py"
TOKEN_REFRESH_SCRIPT = REPO_ROOT / "apps" / "journaling" / "upstox_token_refresh.py"
PYTHON_BIN = REPO_ROOT / ".venv" / "bin" / "python"
MORNING_JSON = REPO_ROOT / "data" / "reports" / "premarket" / "morning_brief_latest.json"
LIVE_JSON = REPO_ROOT / "data" / "reports" / "premarket" / "live" / "live_analysis_latest.json"
LIVE_OUTPUT_DIR = REPO_ROOT / "data" / "reports" / "premarket" / "live"
NON_ALERT_STATUSES = {"intact", "strengthened", "no_comparison", "unknown"}


@dataclass(slots=True)
class ThesisCheck:
    scope: str
    symbol: str
    thesis_status: str
    summary_text: str
    current_price: float
    reference_price: float
    delta_pct: float
    expected_direction: Optional[str] = None
    current_bucket: Optional[str] = None
    morning_rank: Optional[int] = None
    predicted_bias: Optional[str] = None
    current_zone: Optional[str] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live trading-day watch with Telegram alerts.")
    parser.add_argument("--account", default="BALA", choices=("BALA", "NIMMY"))
    parser.add_argument("--env-file", default=str(ENV_FILE))
    parser.add_argument("--state-dir", default=str(RISK_RUNTIME_ROOT))
    parser.add_argument("--interval-seconds", type=int, default=300, help="Watch cadence. Default: 300 seconds.")
    parser.add_argument(
        "--analysis-interval-seconds",
        type=int,
        default=300,
        help="How often to rerun heavy live market analysis. Default: 300 seconds.",
    )
    parser.add_argument("--market-open-time", default="09:00")
    parser.add_argument("--market-close-time", default="23:30")
    parser.add_argument("--top-per-side", type=int, default=3, help="Bullish/bearish stock checks to track. Default: 3.")
    parser.add_argument("--net-loss-alert", type=float, default=3000.0, help="Alert when account net MTM is at or below -value.")
    parser.add_argument("--single-position-loss-alert", type=float, default=2000.0, help="Alert when any single open position P&L is at or below -value.")
    parser.add_argument("--send-telegram-heartbeat", action="store_true", help="Send every cycle summary to Telegram, not just alerts.")
    parser.add_argument("--disable-telegram-send", action="store_true", help="Log Telegram alerts locally instead of sending.")
    parser.add_argument("--once", action="store_true", help="Run one cycle and exit.")
    return parser.parse_args()


class WatchStateStore:
    def __init__(self, state_dir: Path, account: str):
        self.state_dir = state_dir
        self.account = account
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / f"trading_day_watch_{account.lower()}.json"

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default_state()
        try:
            data = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return self._default_state()
        if data.get("session_date") != today_local().isoformat():
            return self._default_state()
        return data

    def save(self, state: dict[str, Any]) -> None:
        self.path.write_text(json.dumps(state, indent=2, default=str))

    @staticmethod
    def _default_state() -> dict[str, Any]:
        return {
            "session_date": today_local().isoformat(),
            "index_statuses": {},
            "stock_statuses": {},
            "net_loss_alert_sent": False,
            "single_position_alerts": [],
            "last_terminal_summary": None,
            "last_alert_at": None,
        }


class TradingDayWatch:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.account = args.account.upper()
        self.env_file = Path(args.env_file)
        self.state_store = WatchStateStore(Path(args.state_dir), self.account)
        self.state = self.state_store.load()
        self.open_time = parse_hhmm(args.market_open_time)
        self.close_time = parse_hhmm(args.market_close_time)
        self.analysis_interval_seconds = max(60, int(args.analysis_interval_seconds))
        self._cached_morning_payload: dict[str, Any] | None = None
        self._cached_live_payload: dict[str, Any] | None = None
        self._last_analysis_run_at: datetime | None = None

        values = env_values(self.env_file)
        self.alert_chat_id = load_alert_chat_id(values)
        self.telegram = None
        token = values.get("TELEGRAM_BOT_TOKEN")
        if token and self.alert_chat_id:
            self.telegram = TelegramClient(load_telegram_token(values), disable_send=args.disable_telegram_send)

        self.upstox = UpstoxRiskClient(
            env_file=self.env_file,
            account=self.account,
            dry_run_close=False,
        )

    def _run_python_script(self, script: Path, *script_args: str, timeout: int = 300) -> subprocess.CompletedProcess[str]:
        if not PYTHON_BIN.exists():
            raise RuntimeError(f"Missing repo python runtime: {PYTHON_BIN}")
        result = subprocess.run(
            [str(PYTHON_BIN), str(script), *script_args],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return result

    def _refresh_tokens(self) -> None:
        logger.info("[%s] Refreshing Upstox tokens before retry...", self.account)
        result = self._run_python_script(TOKEN_REFRESH_SCRIPT, "--account", "ALL", timeout=240)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Token refresh failed: {detail}")

    def _load_json(self, path: Path) -> dict[str, Any]:
        return json.loads(path.read_text())

    def _load_last_meaningful_live_payload(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        candidates = sorted(LIVE_OUTPUT_DIR.glob("live_analysis_*.json"), reverse=True)
        for path in candidates:
            try:
                payload = self._load_json(path)
            except Exception:
                continue
            if not self._json_is_for_today(payload):
                continue
            index_checks, bullish_checks, bearish_checks = self._parse_checks(payload, self.args.top_per_side)
            if self._has_meaningful_checks(index_checks, bullish_checks, bearish_checks):
                morning_payload = self.ensure_morning_brief()
                logger.info("[%s] Bootstrapping watcher cache from prior meaningful live-analysis file %s", self.account, path.name)
                return morning_payload, payload
        return None, None

    def _json_is_for_today(self, payload: dict[str, Any]) -> bool:
        generated_at = str(payload.get("generated_at") or "")
        return generated_at[:10] == today_local().isoformat()

    def ensure_morning_brief(self) -> dict[str, Any]:
        if MORNING_JSON.exists():
            payload = self._load_json(MORNING_JSON)
            if self._json_is_for_today(payload):
                return payload

        logger.info("Morning brief is missing/stale for today. Running a fresh brief...")
        result = self._run_python_script(MORNING_BRIEF_SCRIPT, timeout=360)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Morning brief failed: {detail}")
        return self._load_json(MORNING_JSON)

    def run_live_analysis(self) -> dict[str, Any]:
        result = self._run_python_script(LIVE_ANALYSIS_SCRIPT, timeout=360)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            logger.warning("Live analysis failed once: %s", detail)
            self._refresh_tokens()
            result = self._run_python_script(LIVE_ANALYSIS_SCRIPT, timeout=360)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(f"Live analysis failed: {detail}")
        return self._load_json(LIVE_JSON)

    def _should_refresh_analysis(self) -> bool:
        if self._cached_morning_payload is None or self._cached_live_payload is None:
            return True
        if self._last_analysis_run_at is None:
            return True
        elapsed = (now_local() - self._last_analysis_run_at).total_seconds()
        return elapsed >= self.analysis_interval_seconds

    @staticmethod
    def _extract_global_bias(morning_payload: dict[str, Any]) -> str:
        for prediction in morning_payload.get("predictions", []):
            if prediction.get("symbol") == "NIFTY_CONTEXT":
                return str(prediction.get("predicted_direction") or prediction.get("regime_label") or "unknown").upper()
        section = (((morning_payload.get("sections") or {}).get("global") or {}).get("data") or {})
        return str(section.get("overall_bias") or "UNKNOWN").upper()

    @staticmethod
    def _parse_checks(live_payload: dict[str, Any], top_per_side: int) -> tuple[list[ThesisCheck], list[ThesisCheck], list[ThesisCheck]]:
        index_checks: list[ThesisCheck] = []
        bullish_checks: list[ThesisCheck] = []
        bearish_checks: list[ThesisCheck] = []
        for item in live_payload.get("checks", []):
            details = item.get("details") or {}
            check = ThesisCheck(
                scope=str(item.get("scope") or ""),
                symbol=str(item.get("symbol") or ""),
                thesis_status=str(item.get("thesis_status") or "unknown"),
                summary_text=str(item.get("summary_text") or ""),
                current_price=float(item.get("current_price") or 0.0),
                reference_price=float(item.get("reference_price") or 0.0),
                delta_pct=float(item.get("delta_pct") or 0.0),
                expected_direction=details.get("expected_direction"),
                current_bucket=details.get("current_bucket"),
                morning_rank=details.get("morning_rank"),
                predicted_bias=details.get("predicted_bias"),
                current_zone=details.get("current_zone"),
            )
            if check.scope == "index":
                index_checks.append(check)
            elif check.scope == "fno":
                if check.expected_direction == "bullish":
                    bullish_checks.append(check)
                elif check.expected_direction == "bearish":
                    bearish_checks.append(check)

        bullish_checks.sort(key=lambda item: item.morning_rank or 999)
        bearish_checks.sort(key=lambda item: item.morning_rank or 999)
        return index_checks, bullish_checks[:top_per_side], bearish_checks[:top_per_side]

    @staticmethod
    def _position_matches_symbol(snapshot: MTMSnapshot, symbol: str) -> bool:
        token = symbol.upper()
        return any(token in position.symbol.upper() for position in snapshot.positions)

    def _build_terminal_summary(
        self,
        global_bias: str,
        index_checks: list[ThesisCheck],
        bullish_checks: list[ThesisCheck],
        bearish_checks: list[ThesisCheck],
        mtm_snapshot: MTMSnapshot,
    ) -> str:
        lines = [
            "",
            "=" * 90,
            f"TRADING DAY WATCH | {now_local().strftime('%Y-%m-%d %H:%M:%S IST')}",
            "=" * 90,
            f"Global Bias: {global_bias}",
            "",
            "Indices:",
        ]
        for check in index_checks:
            bias = check.predicted_bias.upper() if check.predicted_bias else "UNKNOWN"
            zone = check.current_zone or "-"
            lines.append(
                f"- {check.symbol}: {check.thesis_status.upper()} | bias {bias} | zone {zone} | spot {check.current_price:,.2f}"
            )

        lines.extend(["", "Bullish Watch:", *[
            f"- {check.symbol}: {check.thesis_status.upper()} | price {check.current_price:,.2f}"
            for check in bullish_checks
        ]])
        lines.extend(["", "Bearish Watch:", *[
            f"- {check.symbol}: {check.thesis_status.upper()} | price {check.current_price:,.2f}"
            for check in bearish_checks
        ]])

        lines.extend([
            "",
            f"{self.account} MTM: Net {format_money(mtm_snapshot.net_pnl)} | Realised {format_money(mtm_snapshot.realised_pnl)} | "
            f"Unrealised {format_money(mtm_snapshot.unrealised_pnl)} | Open {mtm_snapshot.open_positions}",
            summarize_positions(mtm_snapshot),
            "=" * 90,
        ])
        return "\n".join(lines)

    @staticmethod
    def _has_meaningful_checks(
        index_checks: list[ThesisCheck],
        bullish_checks: list[ThesisCheck],
        bearish_checks: list[ThesisCheck],
    ) -> bool:
        relevant = [*index_checks, *bullish_checks, *bearish_checks]
        if not relevant:
            return False
        return any(check.thesis_status not in {"no_comparison", "unknown"} for check in relevant)

    @staticmethod
    def _has_meaningful_index_checks(index_checks: list[ThesisCheck]) -> bool:
        if not index_checks:
            return False
        return any(check.thesis_status not in {"no_comparison", "unknown"} for check in index_checks)

    def _build_unavailable_summary(self, global_bias: str, mtm_snapshot: MTMSnapshot) -> str:
        lines = [
            "",
            "=" * 90,
            f"TRADING DAY WATCH | {now_local().strftime('%Y-%m-%d %H:%M:%S IST')}",
            "=" * 90,
            f"Global Bias: {global_bias}",
            "",
            "Market structure temporarily unavailable from live-analysis payload.",
            "Skipping thesis-change alerts for this cycle and preserving the last meaningful market state.",
            "",
            f"{self.account} MTM: Net {format_money(mtm_snapshot.net_pnl)} | Realised {format_money(mtm_snapshot.realised_pnl)} | "
            f"Unrealised {format_money(mtm_snapshot.unrealised_pnl)} | Open {mtm_snapshot.open_positions}",
            summarize_positions(mtm_snapshot),
            "=" * 90,
        ]
        return "\n".join(lines)

    def _get_analysis_payloads(self) -> tuple[dict[str, Any], dict[str, Any]]:
        if not self._should_refresh_analysis():
            return self._cached_morning_payload or {}, self._cached_live_payload or {}

        morning_payload = self.ensure_morning_brief()
        live_payload = self.run_live_analysis()
        index_checks, bullish_checks, bearish_checks = self._parse_checks(live_payload, self.args.top_per_side)

        if self._has_meaningful_checks(index_checks, bullish_checks, bearish_checks) and self._has_meaningful_index_checks(index_checks):
            self._cached_morning_payload = morning_payload
            self._cached_live_payload = live_payload
            self._last_analysis_run_at = now_local()
            return morning_payload, live_payload

        if self._cached_morning_payload is not None and self._cached_live_payload is not None:
            logger.warning(
                "[%s] Fresh live-analysis payload was incomplete; keeping the last meaningful cached market state.",
                self.account,
            )
            return self._cached_morning_payload, self._cached_live_payload

        fallback_morning, fallback_live = self._load_last_meaningful_live_payload()
        if fallback_morning is not None and fallback_live is not None:
            self._cached_morning_payload = fallback_morning
            self._cached_live_payload = fallback_live
            self._last_analysis_run_at = now_local()
            logger.warning(
                "[%s] Fresh live-analysis payload was incomplete; using the last meaningful saved live-analysis snapshot instead.",
                self.account,
            )
            return fallback_morning, fallback_live

        self._cached_morning_payload = morning_payload
        self._cached_live_payload = live_payload
        self._last_analysis_run_at = now_local()
        return morning_payload, live_payload

    def _send_alert(self, text: str) -> None:
        logger.info("ALERT: %s", text.replace("\n", " | "))
        if not self.telegram or not self.alert_chat_id:
            return
        self.telegram.send_message(self.alert_chat_id, text)
        self.state["last_alert_at"] = now_local().isoformat()

    def _market_alerts(
        self,
        index_checks: list[ThesisCheck],
        bullish_checks: list[ThesisCheck],
        bearish_checks: list[ThesisCheck],
        mtm_snapshot: MTMSnapshot,
    ) -> list[str]:
        alerts: list[str] = []

        if mtm_snapshot.net_pnl <= -abs(self.args.net_loss_alert) and not self.state.get("net_loss_alert_sent"):
            alerts.append(
                "\n".join(
                    [
                        f"*{self.account} Trading Watch*",
                        f"Net MTM alert: `{format_money(mtm_snapshot.net_pnl)}`",
                        f"Threshold breached: `-{abs(self.args.net_loss_alert):,.2f}`",
                        summarize_positions(mtm_snapshot),
                    ]
                )
            )
            self.state["net_loss_alert_sent"] = True
        elif mtm_snapshot.net_pnl > -abs(self.args.net_loss_alert):
            self.state["net_loss_alert_sent"] = False

        sent_single = set(self.state.get("single_position_alerts", []))
        current_single: set[str] = set()
        for position in mtm_snapshot.positions:
            if position.pnl <= -abs(self.args.single_position_loss_alert):
                key = f"{position.symbol}:{position.pnl:.2f}"
                current_single.add(key)
                if key not in sent_single:
                    alerts.append(
                        "\n".join(
                            [
                                f"*{self.account} Trading Watch*",
                                f"Single-position loss alert: `{position.symbol}`",
                                f"P&L: `{format_money(position.pnl)}` | Qty `{position.quantity}` | LTP `{position.last_price:.2f}`",
                            ]
                        )
                    )
        self.state["single_position_alerts"] = sorted(current_single)

        if mtm_snapshot.open_positions > 0:
            for check in index_checks:
                previous = self.state.get("index_statuses", {}).get(check.symbol)
                if previous != check.thesis_status and check.thesis_status not in NON_ALERT_STATUSES:
                    alerts.append(
                        "\n".join(
                            [
                                f"*{self.account} Trading Watch*",
                                f"Index thesis changed: `{check.symbol}` is now `{check.thesis_status.upper()}`",
                                check.summary_text,
                                summarize_positions(mtm_snapshot),
                            ]
                        )
                    )

            tracked_checks = [*bullish_checks, *bearish_checks]
            for check in tracked_checks:
                if not self._position_matches_symbol(mtm_snapshot, check.symbol):
                    continue
                previous = self.state.get("stock_statuses", {}).get(check.symbol)
                if previous != check.thesis_status and check.thesis_status not in NON_ALERT_STATUSES:
                    alerts.append(
                        "\n".join(
                            [
                                f"*{self.account} Trading Watch*",
                                f"Tracked symbol changed: `{check.symbol}` is now `{check.thesis_status.upper()}`",
                                check.summary_text,
                            ]
                        )
                    )

        return alerts

    def _update_status_state(
        self,
        index_checks: list[ThesisCheck],
        bullish_checks: list[ThesisCheck],
        bearish_checks: list[ThesisCheck],
        summary: str,
    ) -> None:
        previous_index_statuses = dict(self.state.get("index_statuses", {}))
        next_index_statuses = dict(previous_index_statuses)
        for check in index_checks:
            if check.thesis_status not in {"no_comparison", "unknown"}:
                next_index_statuses[check.symbol] = check.thesis_status
        self.state["index_statuses"] = next_index_statuses

        previous_stock_statuses = dict(self.state.get("stock_statuses", {}))
        next_stock_statuses = dict(previous_stock_statuses)
        for check in [*bullish_checks, *bearish_checks]:
            if check.thesis_status not in {"no_comparison", "unknown"}:
                next_stock_statuses[check.symbol] = check.thesis_status
        self.state["stock_statuses"] = next_stock_statuses
        self.state["last_terminal_summary"] = summary
        self.state_store.save(self.state)

    def cycle(self) -> None:
        morning_payload, live_payload = self._get_analysis_payloads()
        mtm_snapshot = self.upstox.build_snapshot()

        global_bias = self._extract_global_bias(morning_payload)
        index_checks, bullish_checks, bearish_checks = self._parse_checks(live_payload, self.args.top_per_side)

        if not self._has_meaningful_checks(index_checks, bullish_checks, bearish_checks):
            summary = self._build_unavailable_summary(global_bias, mtm_snapshot)
            logger.warning(
                "[%s] Live analysis payload did not contain meaningful structured checks; preserving prior thesis state.",
                self.account,
            )
            print(summary, flush=True)
            self.state["last_terminal_summary"] = summary
            self.state_store.save(self.state)
            return

        summary = self._build_terminal_summary(
            global_bias,
            index_checks,
            bullish_checks,
            bearish_checks,
            mtm_snapshot,
        )
        print(summary, flush=True)

        alerts = self._market_alerts(index_checks, bullish_checks, bearish_checks, mtm_snapshot)
        for alert in alerts:
            self._send_alert(alert)

        if self.args.send_telegram_heartbeat and self.telegram and self.alert_chat_id:
            self.telegram.send_message(
                self.alert_chat_id,
                "\n".join(
                    [
                        f"*{self.account} Trading Watch*",
                        f"Time: `{now_local().strftime('%Y-%m-%d %H:%M:%S IST')}`",
                        f"NIFTY/BANKNIFTY/SENSEX: " + " | ".join(
                            f"{check.symbol} `{check.thesis_status}`" for check in index_checks
                        ),
                        f"Net MTM: `{format_money(mtm_snapshot.net_pnl)}`",
                        summarize_positions(mtm_snapshot),
                    ]
                ),
            )

        self._update_status_state(index_checks, bullish_checks, bearish_checks, summary)

    def run_forever(self) -> None:
        logger.info("[%s] Trading-day watch starting. Interval=%ss", self.account, self.args.interval_seconds)
        while True:
            if is_within_hours(self.open_time, self.close_time):
                try:
                    self.cycle()
                except Exception as exc:
                    logger.exception("[%s] Trading-day watch cycle failed: %s", self.account, exc)
                    if self.telegram and self.alert_chat_id:
                        try:
                            self.telegram.send_message(
                                self.alert_chat_id,
                                f"*{self.account} Trading Watch*\nCycle failed: `{str(exc)[:250]}`",
                            )
                        except Exception:
                            logger.exception("Failed to send trading-day watch error alert")
                if self.args.once:
                    return
            elif self.args.once:
                self.cycle()
                return
            else:
                logger.info("[%s] Outside configured market hours. Sleeping...", self.account)
            time.sleep(max(5, self.args.interval_seconds))


def main() -> int:
    args = parse_args()
    watcher = TradingDayWatch(args)
    if args.once:
        watcher.cycle()
        return 0
    watcher.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
