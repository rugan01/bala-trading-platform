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
import re
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
    positions_net_pnl,
    summarize_positions,
    today_local,
)

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
            "missing_plan_alert_sent": False,
            "expiry_pilot": {
                "open_symbols": [],
                "entry_count": 0,
                "reentry_count": 0,
                "seen_open_before": False,
                "alerts_sent": [],
            },
            "plan_activity": {
                "open_keys": [],
                "root_stats": {},
                "idea_counts": {"FO": 0, "COM": 0, "EQ": 0, "TOTAL": 0},
                "afternoon_fno_entries": 0,
                "morning_fno_profit_anchor": None,
                "alerts_sent": [],
            },
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

    def _load_plan_context(self) -> dict[str, Any]:
        if load_runtime_plan is None:
            return {}
        try:
            return load_runtime_plan(self.account, runtime_root=Path(self.args.state_dir), plan_date=today_local())
        except Exception as exc:
            logger.warning("[%s] Failed to load runtime daily plan: %s", self.account, exc)
            return {}

    @staticmethod
    def _parse_plan_time(value: str | None) -> dtime | None:
        if not value:
            return None
        try:
            return parse_hhmm(str(value))
        except Exception:
            return None

    def _expiry_positions(self, mtm_snapshot: MTMSnapshot, plan_context: dict[str, Any]) -> list[Any]:
        expiry_pilot = plan_context.get("expiry_pilot") or {}
        underlying = expiry_pilot.get("underlying")
        if not expiry_pilot.get("enabled") or not underlying or symbol_matches_expiry_underlying is None:
            return []
        return [
            position
            for position in mtm_snapshot.positions
            if symbol_matches_expiry_underlying(position.symbol, str(underlying))
        ]

    def _save_runtime_freeze(self, reason: str) -> None:
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

    @staticmethod
    def _root_for_symbol(symbol: str) -> str:
        normalized = "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())
        for root in ("BANKNIFTY", "NIFTY", "SENSEX", "BSX", "SILVERMIC", "SILVERM", "GOLDM", "GOLD", "CRUDEOILM", "CRUDEOIL", "NATGASMINI", "NATGAS", "ZINCMINI", "ZINC"):
            if normalized.startswith(root):
                if root in {"NIFTY", "BANKNIFTY", "SENSEX", "BSX"}:
                    return "INDEX"
                return root
        match = re.match(r"([A-Z]+)", normalized)
        return match.group(1) if match else normalized or "UNKNOWN"

    @staticmethod
    def _segment_for_position(position: Any) -> str:
        exchange = str(getattr(position, "exchange", "") or "").upper()
        symbol = str(getattr(position, "symbol", "") or "").upper()
        if "MCX" in exchange or any(symbol.startswith(root) for root in ("SILVER", "GOLD", "CRUDE", "NATGAS", "ZINC")):
            return "COM"
        if any(token in symbol for token in ("NIFTY", "SENSEX", "BSX", "BANKNIFTY")):
            return "FO"
        if "FO" in exchange and re.search(r"(CE|PE)$", symbol):
            return "STOCK_OPTIONS"
        if "FO" in exchange:
            return "FO"
        return "EQ"

    def _position_root_stats(self, positions: list[Any]) -> dict[str, dict[str, Any]]:
        stats: dict[str, dict[str, Any]] = {}
        for position in positions:
            segment = self._segment_for_position(position)
            root = self._root_for_symbol(getattr(position, "symbol", ""))
            key = f"{segment}:{root}"
            row = stats.setdefault(
                key,
                {
                    "segment": segment,
                    "root": root,
                    "symbols": [],
                    "abs_qty": 0,
                    "pnl": 0.0,
                },
            )
            row["symbols"].append(position.symbol)
            row["abs_qty"] += abs(int(position.quantity or 0))
            row["pnl"] += float(position.pnl or 0.0)
        return stats

    def _planned_roots(self, plan_context: dict[str, Any]) -> set[str]:
        roots: set[str] = set()
        for idea in plan_context.get("ideas") or []:
            symbol = str(idea.get("symbol") or "").strip()
            if symbol:
                roots.add(self._root_for_symbol(symbol))
        return roots

    def _save_activity_runtime_state(self, activity: dict[str, Any]) -> None:
        if update_runtime_plan_state is None:
            return
        try:
            update_runtime_plan_state(
                self.account,
                {
                    "idea_counts": activity.get("idea_counts"),
                    "afternoon_fno_entries": activity.get("afternoon_fno_entries"),
                    "morning_fno_profit_anchor": activity.get("morning_fno_profit_anchor"),
                },
                runtime_root=Path(self.args.state_dir),
                plan_date=today_local(),
            )
        except Exception as exc:
            logger.warning("[%s] Failed to update runtime plan activity state: %s", self.account, exc)

    @staticmethod
    def _format_plan_context(plan_context: dict[str, Any]) -> list[str]:
        if not plan_context:
            return [
                "Plan: MISSING runtime daily plan. Run morning brief or create_daily_trading_plan.py --runtime-json.",
            ]
        banned = ", ".join(plan_context.get("banned_segments") or []) or "-"
        lines = [
            (
                f"Plan: Tier1 {plan_context.get('tier1_segment', '-')} | "
                f"Tier2 {plan_context.get('tier2_segment') or '-'} | "
                f"Banned {banned}"
            ),
            (
                f"Limits: F&O {plan_context.get('max_equity_index_ideas', 2)} ideas | "
                f"StockOpt {plan_context.get('max_positional_stock_option_campaigns', 2)} active | "
                f"COM {plan_context.get('max_commodity_ideas', 2)} ideas | "
                f"Total {plan_context.get('max_total_ideas', 4)} | "
                f"Hard stop {format_money(float(plan_context.get('hard_daily_stop') or 0))}"
            ),
        ]
        expiry_pilot = plan_context.get("expiry_pilot") or {}
        if expiry_pilot.get("enabled"):
            lines.append(
                (
                    f"Expiry Pilot: {expiry_pilot.get('underlying', '-')} | "
                    f"pilot stop {format_money(float(expiry_pilot.get('session_hard_stop') or 0))} | "
                    f"close {expiry_pilot.get('hard_close_time', '-')}"
                )
            )
        return lines

    def _expiry_pilot_alerts(self, mtm_snapshot: MTMSnapshot, plan_context: dict[str, Any]) -> list[str]:
        expiry_pilot = plan_context.get("expiry_pilot") or {}
        if not expiry_pilot.get("enabled"):
            return []

        positions = self._expiry_positions(mtm_snapshot, plan_context)
        current_symbols = sorted(position.symbol for position in positions)
        pilot_state = self.state.setdefault("expiry_pilot", {})
        previous_symbols = sorted(pilot_state.get("open_symbols") or [])
        previous_set = set(previous_symbols)
        current_set = set(current_symbols)
        new_symbols = sorted(current_set - previous_set)
        closed_symbols = sorted(previous_set - current_set)
        alerts_sent = set(pilot_state.get("alerts_sent") or [])
        alerts: list[str] = []

        now = now_local()
        now_time = now.time()
        no_new_after = self._parse_plan_time(expiry_pilot.get("no_new_entries_after"))
        hard_close_time = self._parse_plan_time(expiry_pilot.get("hard_close_time"))
        underlying = expiry_pilot.get("underlying") or "EXPIRY"

        if current_symbols and not previous_symbols:
            if pilot_state.get("seen_open_before"):
                pilot_state["reentry_count"] = int(pilot_state.get("reentry_count") or 0) + 1
                event_label = "re-entry"
            else:
                pilot_state["entry_count"] = int(pilot_state.get("entry_count") or 0) + 1
                pilot_state["seen_open_before"] = True
                event_label = "entry"
            alerts.append(
                "\n".join(
                    [
                        f"*{self.account} Trading Watch*",
                        f"Expiry pilot {event_label} detected: `{underlying}`",
                        f"Open expiry symbols: `{', '.join(current_symbols)}`",
                        f"Entries: `{pilot_state.get('entry_count', 0)}` / `{expiry_pilot.get('max_new_entries')}` | "
                        f"Re-entries: `{pilot_state.get('reentry_count', 0)}` / `{expiry_pilot.get('max_reentries')}`",
                    ]
                )
            )
        elif current_symbols and new_symbols:
            key = f"expiry_new_leg:{','.join(new_symbols)}"
            if key not in alerts_sent:
                alerts.append(
                    "\n".join(
                        [
                            f"*{self.account} Trading Watch*",
                            f"New expiry leg/adjustment detected in `{underlying}`",
                            f"New symbol(s): `{', '.join(new_symbols)}`",
                            "Confirm this is a hedge/risk reduction, not averaging into the losing side.",
                        ]
                    )
                )
                alerts_sent.add(key)

        if closed_symbols and not current_symbols:
            pilot_state["last_flat_at"] = now.isoformat()

        if no_new_after and new_symbols and now_time >= no_new_after:
            key = "expiry_after_cutoff"
            if key not in alerts_sent:
                alerts.append(
                    "\n".join(
                        [
                            f"*{self.account} Trading Watch*",
                            f"Expiry entry after cutoff detected: `{underlying}`",
                            f"Cutoff: `{expiry_pilot.get('no_new_entries_after')}` | Time: `{now.strftime('%H:%M:%S')}`",
                            "Rule: no new expiry entries after cutoff. Reduce/exit mode only.",
                        ]
                    )
                )
                alerts_sent.add(key)

        max_entries = int(expiry_pilot.get("max_new_entries") or 0)
        if max_entries and int(pilot_state.get("entry_count") or 0) > max_entries:
            key = "expiry_entry_limit"
            if key not in alerts_sent:
                alerts.append(
                    "\n".join(
                        [
                            f"*{self.account} Trading Watch*",
                            "Expiry entry limit breached.",
                            f"Entries: `{pilot_state.get('entry_count')}` / `{max_entries}`",
                            "No more expiry entries today.",
                        ]
                    )
                )
                alerts_sent.add(key)

        max_reentries = int(expiry_pilot.get("max_reentries") or 0)
        if max_reentries and int(pilot_state.get("reentry_count") or 0) > max_reentries:
            key = "expiry_reentry_limit"
            if key not in alerts_sent:
                alerts.append(
                    "\n".join(
                        [
                            f"*{self.account} Trading Watch*",
                            "Expiry re-entry limit breached.",
                            f"Re-entries: `{pilot_state.get('reentry_count')}` / `{max_reentries}`",
                            "This is now recovery-trade territory. Stop new expiry trades.",
                        ]
                    )
                )
                alerts_sent.add(key)

        session_stop = abs(float(expiry_pilot.get("session_hard_stop") or 0))
        expiry_net_pnl = positions_net_pnl(positions)
        if session_stop and current_symbols and expiry_net_pnl <= -session_stop:
            key = "expiry_hard_stop"
            if key not in alerts_sent:
                alerts.append(
                    "\n".join(
                        [
                            f"*{self.account} Trading Watch*",
                            f"Expiry pilot hard stop reached: `{format_money(mtm_snapshot.net_pnl)}`",
                            f"Pilot stop: `-{session_stop:,.2f}`",
                            "Close all expiry positions now. No re-entry.",
                            summarize_positions(mtm_snapshot),
                        ]
                    )
                )
                alerts_sent.add(key)
                pilot_state["hard_stop_reached"] = True
                self._save_runtime_freeze("expiry_pilot_hard_stop")

        if hard_close_time and current_symbols and now_time >= hard_close_time:
            key = "expiry_hard_close_time"
            if key not in alerts_sent:
                alerts.append(
                    "\n".join(
                        [
                            f"*{self.account} Trading Watch*",
                            f"Expiry hard-close time reached: `{expiry_pilot.get('hard_close_time')}`",
                            f"Open expiry symbols: `{', '.join(current_symbols)}`",
                            "Rule: close expiry positions. No gamma gambling after hard-close time.",
                        ]
                    )
                )
                alerts_sent.add(key)

        pilot_state["open_symbols"] = current_symbols
        pilot_state["alerts_sent"] = sorted(alerts_sent)
        self.state["expiry_pilot"] = pilot_state
        return alerts

    def _plan_activity_alerts(self, mtm_snapshot: MTMSnapshot, plan_context: dict[str, Any]) -> list[str]:
        if not plan_context:
            return []

        activity = self.state.setdefault("plan_activity", {})
        previous_keys = set(activity.get("open_keys") or [])
        previous_stats = activity.get("root_stats") or {}
        current_stats = self._position_root_stats(mtm_snapshot.positions)
        current_keys = set(current_stats)
        new_keys = sorted(current_keys - previous_keys)
        closed_keys = sorted(previous_keys - current_keys)
        alerts_sent = set(activity.get("alerts_sent") or [])
        idea_counts = dict(activity.get("idea_counts") or {"FO": 0, "STOCK_OPTIONS": 0, "COM": 0, "EQ": 0, "TOTAL": 0})
        for key in ("FO", "STOCK_OPTIONS", "COM", "EQ", "TOTAL"):
            idea_counts.setdefault(key, 0)

        alerts: list[str] = []
        banned_segments = set(plan_context.get("banned_segments") or [])
        planned_roots = self._planned_roots(plan_context)
        now = now_local()
        now_time = now.time()

        for key in new_keys:
            info = current_stats[key]
            segment = info["segment"]
            root = info["root"]
            idea_counts[segment] = int(idea_counts.get(segment, 0)) + 1
            if segment != "STOCK_OPTIONS":
                idea_counts["TOTAL"] = int(idea_counts.get("TOTAL", 0)) + 1

            if segment == "FO" and now_time >= dtime(12, 0):
                activity["afternoon_fno_entries"] = int(activity.get("afternoon_fno_entries") or 0) + 1

            if segment in banned_segments:
                alert_key = f"banned_segment:{key}"
                if alert_key not in alerts_sent:
                    alerts.append(
                        "\n".join(
                            [
                                f"*{self.account} Trading Watch*",
                                f"Banned segment trade detected: `{segment}`",
                                f"Root: `{root}` | Symbols: `{', '.join(info['symbols'])}`",
                                f"Today banned segments: `{', '.join(sorted(banned_segments))}`",
                            ]
                        )
                    )
                    alerts_sent.add(alert_key)

            if planned_roots and root not in planned_roots and key not in planned_roots:
                alert_key = f"unplanned:{key}"
                if alert_key not in alerts_sent:
                    alerts.append(
                        "\n".join(
                            [
                                f"*{self.account} Trading Watch*",
                                f"Unplanned position cluster detected: `{key}`",
                                f"Symbols: `{', '.join(info['symbols'])}`",
                                "This was not declared in the runtime plan. Confirm it is not an impulse/recovery trade.",
                            ]
                        )
                    )
                    alerts_sent.add(alert_key)
            elif not planned_roots:
                alert_key = f"blank_plan:{key}"
                if alert_key not in alerts_sent:
                    alerts.append(
                        "\n".join(
                            [
                                f"*{self.account} Trading Watch*",
                                f"Position opened while runtime plan ideas are blank: `{key}`",
                                f"Symbols: `{', '.join(info['symbols'])}`",
                                "Fill the runtime/markdown plan before taking more trades.",
                            ]
                        )
                    )
                    alerts_sent.add(alert_key)

        max_fno = int(plan_context.get("max_equity_index_ideas") or 2)
        max_stock_options = int(plan_context.get("max_positional_stock_option_campaigns") or 2)
        max_com = int(plan_context.get("max_commodity_ideas") or 2)
        max_total = int(plan_context.get("max_total_ideas") or 4)
        limit_checks = [
            ("FO", max_fno),
            ("STOCK_OPTIONS", max_stock_options),
            ("COM", max_com),
            ("TOTAL", max_total),
        ]
        for bucket, limit in limit_checks:
            if limit and int(idea_counts.get(bucket, 0)) > limit:
                alert_key = f"idea_limit:{bucket}"
                if alert_key not in alerts_sent:
                    alerts.append(
                        "\n".join(
                            [
                                f"*{self.account} Trading Watch*",
                                f"{bucket} idea limit breached.",
                                f"Count: `{idea_counts.get(bucket)}` / `{limit}`",
                                "No new trades in this bucket. Reduce-or-exit mode only.",
                            ]
                        )
                    )
                    alerts_sent.add(alert_key)

        for key, info in current_stats.items():
            previous = previous_stats.get(key) or {}
            prev_qty = abs(int(previous.get("abs_qty") or 0))
            prev_pnl = float(previous.get("pnl") or 0.0)
            current_qty = abs(int(info.get("abs_qty") or 0))
            if prev_qty > 0 and current_qty != prev_qty and info.get("segment") == "STOCK_OPTIONS":
                stock_policy = plan_context.get("stock_options_policy") or {}
                earliest_review_time = self._parse_plan_time(stock_policy.get("earliest_normal_review_time") or "11:00")
                if earliest_review_time and now_time < earliest_review_time:
                    alert_key = f"stock_option_early_change:{key}:{prev_qty}->{current_qty}"
                    if alert_key not in alerts_sent:
                        alerts.append(
                            "\n".join(
                                [
                                    f"*{self.account} Trading Watch*",
                                    f"Stock-option early close/adjustment detected: `{key}`",
                                    f"Previous qty `{prev_qty}`; current qty `{current_qty}`.",
                                    f"Rule: no normal stock-option decisions before `{stock_policy.get('earliest_normal_review_time') or '11:00'}`.",
                                    "Because this is a defined-risk campaign, wait for the 2-hour close unless this is a documented emergency.",
                                ]
                            )
                        )
                        alerts_sent.add(alert_key)
                else:
                    alert_key = f"stock_option_confirmation:{key}:{prev_qty}->{current_qty}"
                    if alert_key not in alerts_sent:
                        alerts.append(
                            "\n".join(
                                [
                                    f"*{self.account} Trading Watch*",
                                    f"Stock-option structure changed: `{key}`",
                                    f"Previous qty `{prev_qty}`; current qty `{current_qty}`.",
                                    "Confirm the underlying gave a 2-hour candle close beyond the predefined invalidation level.",
                                    "Do not convert defined risk into open-ended risk.",
                                ]
                            )
                        )
                        alerts_sent.add(alert_key)
            if prev_qty > 0 and current_qty > prev_qty and prev_pnl < 0:
                if info.get("segment") == "STOCK_OPTIONS":
                    alert_key = f"stock_option_structure_changed:{key}:{current_qty}"
                    if alert_key not in alerts_sent:
                        alerts.append(
                            "\n".join(
                                [
                                    f"*{self.account} Trading Watch*",
                                    f"Stock-option structure changed: `{key}`",
                                    f"Previous qty `{prev_qty}` with P&L `{format_money(prev_pnl)}`; current qty `{current_qty}`.",
                                    "Confirm this completed a planned spread/hedge and did not add naked directional risk.",
                                ]
                            )
                        )
                        alerts_sent.add(alert_key)
                    continue
                alert_key = f"possible_averaging:{key}:{current_qty}"
                if alert_key not in alerts_sent:
                    alerts.append(
                        "\n".join(
                            [
                                f"*{self.account} Trading Watch*",
                                f"Possible averaging detected: `{key}`",
                                f"Previous qty `{prev_qty}` with P&L `{format_money(prev_pnl)}`; current qty `{current_qty}`.",
                                "If this is not a hedge that reduces risk, close the rescue add immediately.",
                            ]
                        )
                    )
                    alerts_sent.add(alert_key)

        if closed_keys:
            activity["last_closed_keys"] = closed_keys
            activity["last_closed_at"] = now.isoformat()
            stock_policy = plan_context.get("stock_options_policy") or {}
            earliest_review_time = self._parse_plan_time(stock_policy.get("earliest_normal_review_time") or "11:00")
            for key in closed_keys:
                previous = previous_stats.get(key) or {}
                if previous.get("segment") != "STOCK_OPTIONS":
                    continue
                if earliest_review_time and now_time < earliest_review_time:
                    alert_key = f"stock_option_early_close:{key}"
                    if alert_key not in alerts_sent:
                        alerts.append(
                            "\n".join(
                                [
                                    f"*{self.account} Trading Watch*",
                                    f"Stock-option campaign closed before `{stock_policy.get('earliest_normal_review_time') or '11:00'}`: `{key}`",
                                    "Rule: no opening-volatility close/adjustment for defined-risk stock-option campaigns.",
                                    "If this was not an emergency, document it as a process violation.",
                                ]
                            )
                        )
                        alerts_sent.add(alert_key)

        if plan_context.get("morning_profit_lock_enabled"):
            anchor = activity.get("morning_fno_profit_anchor")
            if anchor is None and now_time >= dtime(11, 59):
                anchor_value = float(mtm_snapshot.net_pnl)
                if anchor_value > 0:
                    activity["morning_fno_profit_anchor"] = anchor_value
                    anchor = anchor_value
            if anchor is not None and now_time >= dtime(12, 0):
                allowed_giveback = min(float(anchor) * 0.25, float(plan_context.get("max_afternoon_fno_giveback") or 1500))
                giveback = float(anchor) - float(mtm_snapshot.net_pnl)
                if giveback >= allowed_giveback:
                    alert_key = "morning_profit_giveback_lock"
                    if alert_key not in alerts_sent:
                        alerts.append(
                            "\n".join(
                                [
                                    f"*{self.account} Trading Watch*",
                                    "Morning-profit giveback lock triggered.",
                                    f"Morning anchor: `{format_money(float(anchor))}`",
                                    f"Current MTM: `{format_money(mtm_snapshot.net_pnl)}`",
                                    f"Allowed giveback: `{format_money(allowed_giveback)}`",
                                    "Stop new F&O trades. Protect the green morning.",
                                ]
                            )
                        )
                        alerts_sent.add(alert_key)
                if int(activity.get("afternoon_fno_entries") or 0) > 1:
                    alert_key = "afternoon_fno_entry_limit"
                    if alert_key not in alerts_sent:
                        alerts.append(
                            "\n".join(
                                [
                                    f"*{self.account} Trading Watch*",
                                    "Afternoon F&O entry limit breached after morning-profit lock.",
                                    f"Afternoon F&O entries: `{activity.get('afternoon_fno_entries')}` / `1`",
                                    "No more F&O entries today.",
                                ]
                            )
                        )
                        alerts_sent.add(alert_key)

        activity["open_keys"] = sorted(current_keys)
        activity["root_stats"] = current_stats
        activity["idea_counts"] = idea_counts
        activity["alerts_sent"] = sorted(alerts_sent)
        self.state["plan_activity"] = activity
        self._save_activity_runtime_state(activity)
        return alerts

    def _build_terminal_summary(
        self,
        global_bias: str,
        index_checks: list[ThesisCheck],
        bullish_checks: list[ThesisCheck],
        bearish_checks: list[ThesisCheck],
        mtm_snapshot: MTMSnapshot,
        plan_context: dict[str, Any],
    ) -> str:
        lines = [
            "",
            "=" * 90,
            f"TRADING DAY WATCH | {now_local().strftime('%Y-%m-%d %H:%M:%S IST')}",
            "=" * 90,
            f"Global Bias: {global_bias}",
            *self._format_plan_context(plan_context),
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

    def _build_unavailable_summary(
        self,
        global_bias: str,
        mtm_snapshot: MTMSnapshot,
        plan_context: dict[str, Any],
    ) -> str:
        lines = [
            "",
            "=" * 90,
            f"TRADING DAY WATCH | {now_local().strftime('%Y-%m-%d %H:%M:%S IST')}",
            "=" * 90,
            f"Global Bias: {global_bias}",
            *self._format_plan_context(plan_context),
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
        plan_context: dict[str, Any],
    ) -> list[str]:
        alerts: list[str] = []

        if not plan_context and not self.state.get("missing_plan_alert_sent"):
            alerts.append(
                "\n".join(
                    [
                        f"*{self.account} Trading Watch*",
                        "Runtime daily plan missing.",
                        "Run `morning_brief.py` or `create_daily_trading_plan.py --runtime-json` so plan-aware guardrails can work.",
                    ]
                )
            )
            self.state["missing_plan_alert_sent"] = True
        elif plan_context:
            self.state["missing_plan_alert_sent"] = False

        net_loss_threshold = abs(float(plan_context.get("hard_daily_stop") or self.args.net_loss_alert))
        if mtm_snapshot.net_pnl <= -net_loss_threshold and not self.state.get("net_loss_alert_sent"):
            alerts.append(
                "\n".join(
                    [
                        f"*{self.account} Trading Watch*",
                        f"Net MTM alert: `{format_money(mtm_snapshot.net_pnl)}`",
                        f"Threshold breached: `-{net_loss_threshold:,.2f}`",
                        summarize_positions(mtm_snapshot),
                    ]
                )
            )
            self.state["net_loss_alert_sent"] = True
        elif mtm_snapshot.net_pnl > -net_loss_threshold:
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

        alerts.extend(self._plan_activity_alerts(mtm_snapshot, plan_context))
        alerts.extend(self._expiry_pilot_alerts(mtm_snapshot, plan_context))

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
        plan_context = self._load_plan_context()
        mtm_snapshot = self.upstox.build_snapshot()

        global_bias = self._extract_global_bias(morning_payload)
        index_checks, bullish_checks, bearish_checks = self._parse_checks(live_payload, self.args.top_per_side)

        if not self._has_meaningful_checks(index_checks, bullish_checks, bearish_checks):
            summary = self._build_unavailable_summary(global_bias, mtm_snapshot, plan_context)
            logger.warning(
                "[%s] Live analysis payload did not contain meaningful structured checks; preserving prior thesis state.",
                self.account,
            )
            print(summary, flush=True)
            alerts = self._market_alerts([], [], [], mtm_snapshot, plan_context)
            for alert in alerts:
                self._send_alert(alert)
            self.state["last_terminal_summary"] = summary
            self.state_store.save(self.state)
            return

        summary = self._build_terminal_summary(
            global_bias,
            index_checks,
            bullish_checks,
            bearish_checks,
            mtm_snapshot,
            plan_context,
        )
        print(summary, flush=True)

        alerts = self._market_alerts(index_checks, bullish_checks, bearish_checks, mtm_snapshot, plan_context)
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
                            f"Expiry pilot MTM: `{format_money(expiry_net_pnl)}`",
                            f"Account net MTM: `{format_money(mtm_snapshot.net_pnl)}`",
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
