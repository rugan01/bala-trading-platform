#!/usr/bin/env python3.11
from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

REPO_ROOT = Path(__file__).resolve().parents[4]
ENV_FILE_DEFAULT = str(REPO_ROOT / ".env")
OUTPUT_DIR_DEFAULT = str(REPO_ROOT / "data" / "legacy-analyzers" / "index-expiry")
INDEX_KEY = "BSE_INDEX|SENSEX"
QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
HISTORICAL_URL = "https://api.upstox.com/v3/historical-candle"
OPTION_CONTRACT_URL = "https://api.upstox.com/v2/option/contract"
OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"
SUPPORTED_ACCOUNTS = ("BALA", "NIMMY")


@dataclass
class Candle:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    oi: float = 0.0

    @property
    def dt(self) -> datetime:
        return datetime.fromisoformat(self.ts)


@dataclass
class PivotLevels:
    prev_high: float
    prev_low: float
    prev_close: float
    pivot: float
    bc: float
    tc: float
    r1: float
    r2: float
    r3: float
    s1: float
    s2: float
    s3: float
    cpr_width: float
    cpr_width_pct: float


@dataclass
class BaselineSnapshot:
    captured_at: str
    spot_916: float
    strike: float
    ce_key: str
    pe_key: str
    ce_symbol: str
    pe_symbol: str
    ce_916: float | None
    pe_916: float | None
    combined_916: float
    lot_size: int
    source: str = "live_quote_snapshot"
    baseline_lag_seconds: float = 0.0


@dataclass
class PositionState:
    status: str
    expiry: str
    strike: float
    ce_key: str
    pe_key: str
    ce_symbol: str
    pe_symbol: str
    baseline_spot: float
    entry_time: str
    entry_ce: float
    entry_pe: float
    entry_combined: float
    lot_size: int
    lots: int
    max_loss_rupees: float
    stop_high: float
    stop_low: float
    last_spot: float | None = None
    last_combined: float | None = None
    pnl_points: float | None = None
    pnl_rupees: float | None = None
    exit_reason: str | None = None
    exit_time: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor the SENSEX expiry short-straddle sellers-day setup.")
    parser.add_argument("--env-file", default=ENV_FILE_DEFAULT)
    parser.add_argument("--output-dir", default=OUTPUT_DIR_DEFAULT)
    parser.add_argument("--account", choices=SUPPORTED_ACCOUNTS, default="NIMMY")
    parser.add_argument("--analysis-only", action="store_true", help="Only write the pre-open/spot/option analysis and exit.")
    parser.add_argument("--poll-seconds", type=int, default=300, help="Monitoring interval after entry. Default: 300 seconds.")
    parser.add_argument("--premium-threshold-pct", type=float, default=0.005, help="ATM straddle premium threshold as a fraction of spot. Default: 0.5%%.")
    parser.add_argument("--spot-stop-points", type=float, default=500.0, help="Underlying stop from 9:16 baseline spot. Default: 500 points.")
    parser.add_argument("--entry-eval-time", default="09:35", help="Time to evaluate sellers-day decay.")
    parser.add_argument("--baseline-time", default="09:16", help="Time used for baseline ATM and premium snapshot.")
    parser.add_argument("--force-exit-time", default="15:25", help="Time to force-close the paper trade.")
    parser.add_argument("--lots", type=int, default=1, help="Number of lots to paper-monitor. Default: 1.")
    parser.add_argument("--max-loss-rupees", type=float, default=2000.0, help="Hard paper max-loss exit in rupees. Default: 2000.")
    parser.add_argument("--active-risk-poll-seconds", type=int, default=5, help="Polling interval while a position is open. Default: 5 seconds.")
    parser.add_argument("--loss-alert-levels", default="1000,1500,1800,2000", help="Comma-separated rupee loss alert levels.")
    parser.add_argument("--spot-alert-distances", default="100,50,25,0", help="Comma-separated spot-distance-to-stop alert levels.")
    parser.add_argument("--profit-alert-levels", default="500,1000,1500,2000", help="Comma-separated rupee profit alert levels.")
    parser.add_argument("--disable-telegram-alerts", action="store_true", help="Disable Telegram alerts even if env keys are present.")
    parser.add_argument("--pre-entry-poll-seconds", type=int, default=5, help="Live polling interval before entry/baseline capture. Default: 5 seconds.")
    parser.add_argument("--baseline-live-capture-grace-minutes", type=int, default=20, help="Last acceptable live snapshot window after baseline time. Default: 20 minutes.")
    parser.add_argument("--max-baseline-lag-seconds", type=int, default=60, help="Do not enter if baseline was captured more than this many seconds after baseline time. Default: 60.")
    parser.add_argument("--baseline-live-fallback-grace-minutes", type=int, default=None, help=argparse.SUPPRESS)
    parser.add_argument("--manual-baseline-spot", type=float, default=None, help="Authoritative 09:16 spot captured externally from live market.")
    parser.add_argument("--manual-baseline-strike", type=float, default=None, help="Authoritative 09:16 ATM strike to monitor.")
    parser.add_argument("--manual-baseline-combined", type=float, default=None, help="Authoritative 09:16 CE+PE combined premium.")
    parser.add_argument("--manual-baseline-ce", type=float, default=None, help="Optional authoritative 09:16 CE premium.")
    parser.add_argument("--manual-baseline-pe", type=float, default=None, help="Optional authoritative 09:16 PE premium.")
    parser.add_argument("--manual-baseline-time", default="09:16", help="Display time for manual baseline. Default: 09:16.")
    parser.add_argument("--reset-position-state", action="store_true", help="Ignore any saved paper position for today and start fresh.")
    return parser.parse_args()


def read_env(path: Path) -> str:
    return path.read_text()


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


def load_refresh_timestamp(env_text: str, account: str) -> str | None:
    primary = env_value(env_text, f"UPSTOX_{account}_TOKEN_REFRESHED_AT")
    if primary:
        return primary
    if account == "BALA":
        return env_value(env_text, "UPSTOX_TOKEN_REFRESHED_AT")
    return None


def write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str))


def read_json(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None


def today_ist() -> date:
    return datetime.now().astimezone().date()


def parse_hhmm(value: str) -> dtime:
    hour, minute = value.split(":")
    return dtime(hour=int(hour), minute=int(minute))


def format_money(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def parse_float_list(value: str) -> list[float]:
    result = []
    for part in value.split(","):
        part = part.strip()
        if part:
            result.append(float(part))
    return sorted(result)


def load_sent_alerts(state_file: Path, session_date: date) -> set[str]:
    state = read_json(state_file)
    if not state:
        return set()
    alerts = state.get("alerts", {})
    if alerts.get("session_date") != session_date.isoformat():
        return set()
    return set(alerts.get("sent", []))


def telegram_credentials(env_text: str) -> tuple[str | None, str | None]:
    return env_value(env_text, "TELEGRAM_BOT_TOKEN"), env_value(env_text, "TELEGRAM_CHAT_ID")


def send_telegram_alert(token: str, chat_id: str, message: str) -> str | None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=10,
        )
        response.raise_for_status()
        return None
    except Exception as exc:
        return str(exc)


class UpstoxClient:
    def __init__(self, token: str):
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }

    def _get(self, url: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        response = requests.get(url, headers=self.headers, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def ensure_connection(self) -> dict[str, Any]:
        body = requests.get(QUOTE_URL, headers=self.headers, params={"instrument_key": INDEX_KEY}, timeout=20)
        if body.status_code == 401:
            payload = body.json()
            message = payload.get("errors", [{}])[0].get("message", "Invalid token")
            raise RuntimeError(message)
        body.raise_for_status()
        data = body.json().get("data", {})
        quote = data.get("BSE_INDEX:SENSEX")
        if not quote:
            raise RuntimeError("SENSEX quote not returned even though the request succeeded.")
        return quote

    def quote(self, instrument_key: str) -> dict[str, Any]:
        data = self._get(QUOTE_URL, {"instrument_key": instrument_key}).get("data", {})
        direct = data.get(instrument_key)
        if direct:
            return direct
        alt = data.get(instrument_key.replace("|", ":"))
        if alt:
            return alt
        for map_key, value in data.items():
            if value.get("instrument_token") == instrument_key:
                return value
        raise RuntimeError(f"No quote returned for {instrument_key}")

    def candles(self, instrument_key: str, unit: str, interval: str, from_date: date, to_date: date) -> list[Candle]:
        encoded = quote(instrument_key, safe="")
        url = f"{HISTORICAL_URL}/{encoded}/{unit}/{interval}/{to_date.isoformat()}/{from_date.isoformat()}"
        payload = self._get(url)
        rows = payload.get("data", {}).get("candles", [])
        candles = [
            Candle(
                ts=row[0],
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=float(row[5]),
                oi=float(row[6]) if len(row) > 6 and row[6] is not None else 0.0,
            )
            for row in rows
        ]
        candles.sort(key=lambda item: item.ts)
        return candles

    def option_contracts(self, underlying_key: str) -> list[dict[str, Any]]:
        return self._get(OPTION_CONTRACT_URL, {"instrument_key": underlying_key}).get("data", [])

    def option_chain(self, underlying_key: str, expiry: str) -> list[dict[str, Any]]:
        return self._get(OPTION_CHAIN_URL, {"instrument_key": underlying_key, "expiry_date": expiry}).get("data", [])


def compute_pivots(prev_day: Candle) -> PivotLevels:
    high = prev_day.high
    low = prev_day.low
    close = prev_day.close
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
    return PivotLevels(
        prev_high=high,
        prev_low=low,
        prev_close=close,
        pivot=round(pivot, 2),
        bc=round(bc, 2),
        tc=round(tc, 2),
        r1=round(r1, 2),
        r2=round(r2, 2),
        r3=round(r3, 2),
        s1=round(s1, 2),
        s2=round(s2, 2),
        s3=round(s3, 2),
        cpr_width=round(cpr_width, 2),
        cpr_width_pct=round((cpr_width / close) * 100, 4),
    )


def nearest_expiry(contracts: list[dict[str, Any]], as_of: date) -> str:
    expiries = sorted({row["expiry"] for row in contracts if row.get("expiry") >= as_of.isoformat()})
    if not expiries:
        raise RuntimeError("No live SENSEX expiries found from option contracts.")
    return expiries[0]


def strike_contract_map(contracts: list[dict[str, Any]], expiry: str) -> dict[tuple[float, str], dict[str, Any]]:
    result: dict[tuple[float, str], dict[str, Any]] = {}
    for row in contracts:
        if row.get("expiry") != expiry:
            continue
        strike = float(row["strike_price"])
        option_type = row["instrument_type"]
        result[(strike, option_type)] = row
    return result


def option_mark(market_data: dict[str, Any]) -> float:
    ltp = float(market_data.get("ltp") or 0.0)
    bid = float(market_data.get("bid_price") or 0.0)
    ask = float(market_data.get("ask_price") or 0.0)
    close = float(market_data.get("close_price") or 0.0)
    if ltp > 0:
        return ltp
    if bid > 0 and ask > 0:
        return round((bid + ask) / 2, 2)
    if ask > 0:
        return ask
    if bid > 0:
        return bid
    return close


def find_chain_row_by_strike(chain: list[dict[str, Any]], strike: float) -> dict[str, Any] | None:
    for row in chain:
        if float(row["strike_price"]) == float(strike):
            return row
    return None


def nearest_chain_row(chain: list[dict[str, Any]], spot: float) -> dict[str, Any]:
    return min(chain, key=lambda row: abs(float(row["strike_price"]) - spot))


def build_preopen_analysis(
    quote: dict[str, Any],
    pivots: PivotLevels,
    expiry: str,
    chain: list[dict[str, Any]],
    premium_threshold_pct: float,
) -> dict[str, Any]:
    spot = float(quote["last_price"])
    atm = nearest_chain_row(chain, spot)
    ce_md = atm["call_options"]["market_data"]
    pe_md = atm["put_options"]["market_data"]
    atm_combined = option_mark(ce_md) + option_mark(pe_md)
    threshold = spot * premium_threshold_pct
    nearby = []
    for row in sorted(chain, key=lambda item: abs(float(item["strike_price"]) - spot))[:5]:
        ce = row["call_options"]["market_data"]
        pe = row["put_options"]["market_data"]
        nearby.append({
            "strike": float(row["strike_price"]),
            "combined": round(option_mark(ce) + option_mark(pe), 2),
            "ce_mark": round(option_mark(ce), 2),
            "pe_mark": round(option_mark(pe), 2),
            "ce_oi": float(ce.get("oi") or 0.0),
            "pe_oi": float(pe.get("oi") or 0.0),
        })
    return {
        "timestamp": quote.get("timestamp"),
        "spot": spot,
        "expiry": expiry,
        "pivot_levels": asdict(pivots),
        "atm_strike_now": float(atm["strike_price"]),
        "atm_combined_now": round(atm_combined, 2),
        "premium_threshold": round(threshold, 2),
        "threshold_satisfied_now": atm_combined > threshold,
        "nearby_straddles": nearby,
    }


def capture_live_baseline_snapshot(
    quote: dict[str, Any],
    chain: list[dict[str, Any]],
    contracts: list[dict[str, Any]],
    expiry: str,
    session_date: date,
    baseline_time: dtime,
    grace_minutes: int,
) -> BaselineSnapshot | None:
    now = datetime.now()
    baseline_dt = datetime.combine(session_date, baseline_time)
    if now < baseline_dt:
        return None
    if now > baseline_dt + timedelta(minutes=grace_minutes):
        return None

    spot = float(quote["last_price"])
    atm = nearest_chain_row(chain, spot)
    strike = float(atm["strike_price"])
    ce_now, pe_now, combined_now = current_leg_marks(chain, strike)
    contract_map = strike_contract_map(contracts, expiry)
    ce_contract = contract_map.get((strike, "CE"))
    pe_contract = contract_map.get((strike, "PE"))
    if not ce_contract or not pe_contract:
        return None

    actual_time = now.strftime("%H:%M:%S")
    return BaselineSnapshot(
        captured_at=f"{session_date.isoformat()} {actual_time}",
        spot_916=round(spot, 2),
        strike=strike,
        ce_key=str(ce_contract["instrument_key"]),
        pe_key=str(pe_contract["instrument_key"]),
        ce_symbol=str(ce_contract["trading_symbol"]),
        pe_symbol=str(pe_contract["trading_symbol"]),
        ce_916=ce_now,
        pe_916=pe_now,
        combined_916=combined_now,
        lot_size=int(float(ce_contract.get("lot_size") or pe_contract.get("lot_size") or 20)),
        source="live_quote_snapshot",
        baseline_lag_seconds=round((now - baseline_dt).total_seconds(), 2),
    )


def build_manual_baseline(
    args: argparse.Namespace,
    contracts: list[dict[str, Any]],
    expiry: str,
    session_date: date,
) -> BaselineSnapshot | None:
    values = (args.manual_baseline_spot, args.manual_baseline_strike, args.manual_baseline_combined)
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise RuntimeError(
            "Manual baseline requires --manual-baseline-spot, --manual-baseline-strike, "
            "and --manual-baseline-combined together."
        )

    strike = float(args.manual_baseline_strike)
    contract_map = strike_contract_map(contracts, expiry)
    ce_contract = contract_map.get((strike, "CE"))
    pe_contract = contract_map.get((strike, "PE"))
    if not ce_contract or not pe_contract:
        raise RuntimeError(f"Could not resolve CE/PE contracts for manual baseline strike {strike:.0f}.")

    ce_value = args.manual_baseline_ce
    pe_value = args.manual_baseline_pe
    if ce_value is not None and pe_value is None:
        pe_value = round(float(args.manual_baseline_combined) - float(ce_value), 2)
    elif pe_value is not None and ce_value is None:
        ce_value = round(float(args.manual_baseline_combined) - float(pe_value), 2)

    return BaselineSnapshot(
        captured_at=f"{session_date.isoformat()} {args.manual_baseline_time}",
        spot_916=round(float(args.manual_baseline_spot), 2),
        strike=strike,
        ce_key=str(ce_contract["instrument_key"]),
        pe_key=str(pe_contract["instrument_key"]),
        ce_symbol=str(ce_contract["trading_symbol"]),
        pe_symbol=str(pe_contract["trading_symbol"]),
        ce_916=round(float(ce_value), 2) if ce_value is not None else None,
        pe_916=round(float(pe_value), 2) if pe_value is not None else None,
        combined_916=round(float(args.manual_baseline_combined), 2),
        lot_size=int(float(ce_contract.get("lot_size") or pe_contract.get("lot_size") or 20)),
        source="manual_user_input",
        baseline_lag_seconds=0.0,
    )


def current_leg_marks(chain: list[dict[str, Any]], strike: float) -> tuple[float, float, float]:
    row = find_chain_row_by_strike(chain, strike)
    if not row:
        raise RuntimeError(f"Strike {strike} not found in current option chain.")
    ce = option_mark(row["call_options"]["market_data"])
    pe = option_mark(row["put_options"]["market_data"])
    return round(ce, 2), round(pe, 2), round(ce + pe, 2)


def build_baseline_decay_tracker(
    baseline: BaselineSnapshot | None,
    chain: list[dict[str, Any]],
    lots: int,
) -> dict[str, Any] | None:
    if baseline is None:
        return None
    ce_now, pe_now, combined_now = current_leg_marks(chain, baseline.strike)
    decay_points = round(baseline.combined_916 - combined_now, 2)
    return {
        "strike": baseline.strike,
        "ce_now": ce_now,
        "pe_now": pe_now,
        "combined_now": combined_now,
        "decay_points": decay_points,
        "paper_pnl_rupees_from_916": round(decay_points * baseline.lot_size * lots, 2),
    }


def maybe_enter_position(
    baseline: BaselineSnapshot,
    chain: list[dict[str, Any]],
    eval_time: dtime,
    threshold_pct: float,
    stop_points: float,
    lots: int,
    max_loss_rupees: float,
    max_baseline_lag_seconds: int,
) -> PositionState | None:
    now = datetime.now().time()
    if now < eval_time:
        return None

    ce_now, pe_now, combined_now = current_leg_marks(chain, baseline.strike)
    threshold = baseline.spot_916 * threshold_pct
    if baseline.baseline_lag_seconds > max_baseline_lag_seconds:
        return None
    if baseline.combined_916 <= threshold:
        return None
    if combined_now >= baseline.combined_916:
        return None

    entry_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return PositionState(
        status="OPEN",
        expiry=baseline.captured_at[:10],
        strike=baseline.strike,
        ce_key=baseline.ce_key,
        pe_key=baseline.pe_key,
        ce_symbol=baseline.ce_symbol,
        pe_symbol=baseline.pe_symbol,
        baseline_spot=baseline.spot_916,
        entry_time=entry_time,
        entry_ce=ce_now,
        entry_pe=pe_now,
        entry_combined=combined_now,
        lot_size=baseline.lot_size,
        lots=lots,
        max_loss_rupees=max_loss_rupees,
        stop_high=round(baseline.spot_916 + stop_points, 2),
        stop_low=round(baseline.spot_916 - stop_points, 2),
        last_spot=baseline.spot_916,
        last_combined=combined_now,
    )


def update_position(position: PositionState, spot: float, chain: list[dict[str, Any]], force_exit_time: dtime) -> PositionState:
    ce_now, pe_now, combined_now = current_leg_marks(chain, position.strike)
    pnl_points = round(position.entry_combined - combined_now, 2)
    pnl_rupees = round(pnl_points * position.lot_size * position.lots, 2)
    position.last_spot = round(spot, 2)
    position.last_combined = combined_now
    position.pnl_points = pnl_points
    position.pnl_rupees = pnl_rupees

    now = datetime.now()
    if spot >= position.stop_high:
        position.status = "CLOSED"
        position.exit_reason = "Spot stop hit on upside"
    elif spot <= position.stop_low:
        position.status = "CLOSED"
        position.exit_reason = "Spot stop hit on downside"
    elif pnl_rupees <= -abs(position.max_loss_rupees):
        position.status = "CLOSED"
        position.exit_reason = f"Max loss hit at ₹{abs(position.max_loss_rupees):.2f}"
    elif now.time() >= force_exit_time:
        position.status = "CLOSED"
        position.exit_reason = "Force exit at configured end-of-day time"

    if position.status == "CLOSED" and not position.exit_time:
        position.exit_time = now.strftime("%Y-%m-%d %H:%M:%S")
    return position


def compute_risk_status(position: PositionState | None, spot: float | None) -> dict[str, Any]:
    if position is None:
        return {"action": "NO_POSITION", "message": "No paper position is open."}

    pnl_rupees = float(position.pnl_rupees or 0.0)
    loss_used = max(0.0, -pnl_rupees)
    loss_remaining = round(float(position.max_loss_rupees) - loss_used, 2)
    stop_distance = None
    nearest_stop_side = "-"
    if spot is not None:
        lower_distance = round(float(spot) - position.stop_low, 2)
        upper_distance = round(position.stop_high - float(spot), 2)
        if lower_distance <= upper_distance:
            stop_distance = max(0.0, lower_distance)
            nearest_stop_side = "lower"
        else:
            stop_distance = max(0.0, upper_distance)
            nearest_stop_side = "upper"

    max_loss_combined = round(
        position.entry_combined + (float(position.max_loss_rupees) / (position.lot_size * position.lots)),
        2,
    )

    action = "MONITOR"
    message = "Position is open and inside configured risk limits."
    if position.status == "CLOSED":
        action = "EXIT_NOW"
        message = position.exit_reason or "Position is closed."
    elif spot is not None and (spot <= position.stop_low or spot >= position.stop_high):
        action = "EXIT_NOW"
        message = "Spot stop is hit."
    elif pnl_rupees <= -abs(position.max_loss_rupees):
        action = "EXIT_NOW"
        message = "Max-loss is hit."
    elif loss_remaining <= 200 or (stop_distance is not None and stop_distance <= 25):
        action = "EXIT_READY"
        message = "Very close to stop/max-loss. Be ready to exit immediately."
    elif loss_remaining <= 500 or (stop_distance is not None and stop_distance <= 50):
        action = "DANGER"
        message = "Risk is high. Watch continuously."
    elif loss_remaining <= 1000 or (stop_distance is not None and stop_distance <= 100):
        action = "CAUTION"
        message = "Risk is elevated."

    return {
        "action": action,
        "message": message,
        "spot": round(float(spot), 2) if spot is not None else None,
        "nearest_stop_side": nearest_stop_side,
        "nearest_stop_distance": stop_distance,
        "loss_used": round(loss_used, 2),
        "loss_remaining": loss_remaining,
        "max_loss_combined": max_loss_combined,
        "pnl_rupees": round(pnl_rupees, 2),
        "pnl_points": position.pnl_points,
        "last_combined": position.last_combined,
        "stop_low": position.stop_low,
        "stop_high": position.stop_high,
    }


def build_alert_batch(
    position: PositionState | None,
    risk_status: dict[str, Any],
    loss_levels: list[float],
    spot_distances: list[float],
    profit_levels: list[float],
    sent_alerts: set[str],
) -> tuple[list[str], str | None]:
    if position is None:
        return [], None

    action = str(risk_status.get("action", "MONITOR"))
    if action == "NO_POSITION":
        return [], None

    triggered: list[tuple[str, str]] = []
    loss_used = float(risk_status.get("loss_used") or 0.0)
    pnl_rupees = float(risk_status.get("pnl_rupees") or 0.0)
    stop_distance = risk_status.get("nearest_stop_distance")
    stop_side = risk_status.get("nearest_stop_side")

    for level in loss_levels:
        if loss_used >= level:
            triggered.append((f"loss_{level:g}", f"loss crossed ₹{level:g}"))

    if stop_distance is not None:
        for distance in spot_distances:
            if float(stop_distance) <= distance:
                triggered.append((f"spot_{stop_side}_{distance:g}", f"spot within {distance:g} points of {stop_side} stop"))

    for level in profit_levels:
        if pnl_rupees >= level:
            triggered.append((f"profit_{level:g}", f"profit crossed ₹{level:g}"))

    if action in {"CAUTION", "DANGER", "EXIT_READY", "EXIT_NOW"}:
        triggered.append((f"action_{action}", f"risk action is {action}"))

    new_items = [(key, description) for key, description in triggered if key not in sent_alerts]
    if not new_items:
        return [], None

    keys = [key for key, _ in new_items]
    descriptions = ", ".join(description for _, description in new_items)
    message = "\n".join([
        "SENSEX 0DTE STRADDLE RISK ALERT",
        f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}",
        f"Action: {risk_status.get('action')} - {risk_status.get('message')}",
        f"Triggered: {descriptions}",
        f"Spot: {format_money(risk_status.get('spot'))}",
        f"Stop band: {position.stop_low:.2f} to {position.stop_high:.2f}",
        f"Combined premium: {format_money(position.last_combined)}",
        f"P&L: ₹{format_money(position.pnl_rupees)}",
        f"Loss remaining: ₹{format_money(risk_status.get('loss_remaining'))}",
    ])
    return keys, message


def render_report(
    account: str,
    token_ok: bool,
    token_message: str,
    preopen: dict[str, Any],
    baseline: BaselineSnapshot | None,
    baseline_tracker: dict[str, Any] | None,
    position: PositionState | None,
    max_baseline_lag_seconds: int,
    risk_status: dict[str, Any] | None = None,
    alert_status: dict[str, Any] | None = None,
) -> str:
    piv = preopen["pivot_levels"]
    lines = [
        "# SENSEX Expiry Short Straddle Monitor",
        "",
        f"- Account: `{account}`",
        f"- Connection: `{'live' if token_ok else 'invalid'}`",
        f"- Note: {token_message}",
        f"- Report Time: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}`",
        "",
        "## Market Structure",
        f"- Spot: `{preopen['spot']:.2f}`",
        f"- Expiry: `{preopen['expiry']}`",
        f"- Yesterday Close: `{piv['prev_close']:.2f}`",
        f"- Pivot: `{piv['pivot']:.2f}`",
        f"- BC / TC: `{piv['bc']:.2f}` / `{piv['tc']:.2f}`",
        f"- R1 / S1: `{piv['r1']:.2f}` / `{piv['s1']:.2f}`",
        f"- R2 / S2: `{piv['r2']:.2f}` / `{piv['s2']:.2f}`",
        f"- CPR Width: `{piv['cpr_width']:.2f}` points (`{piv['cpr_width_pct']:.4f}%` of close)",
        "",
        "## ATM Straddle Condition",
        f"- ATM strike now: `{preopen['atm_strike_now']:.0f}`",
        f"- ATM combined premium now: `{preopen['atm_combined_now']:.2f}`",
        f"- 0.5% threshold: `{preopen['premium_threshold']:.2f}`",
        f"- Threshold satisfied now: `{'yes' if preopen['threshold_satisfied_now'] else 'no'}`",
        "",
        "## Nearby Straddles",
    ]
    for row in preopen["nearby_straddles"]:
        lines.append(
            f"- `{row['strike']:.0f}` -> combined `{row['combined']:.2f}` | CE `{row['ce_mark']:.2f}` | PE `{row['pe_mark']:.2f}` | CE OI `{row['ce_oi']:.0f}` | PE OI `{row['pe_oi']:.0f}`"
        )

    lines.extend(["", "## 9:16 Baseline"])
    if baseline is None:
        lines.append("- Baseline not available yet. The script will capture it from live quote and live option-chain snapshots at or after the configured baseline time.")
    else:
        threshold = baseline.spot_916 * 0.005
        lines.extend([
            f"- Captured at: `{baseline.captured_at}`",
            f"- Baseline source: `{baseline.source}`",
            f"- Baseline lag: `{baseline.baseline_lag_seconds:.0f}` seconds",
            f"- Clean entry eligible: `{'yes' if baseline.baseline_lag_seconds <= max_baseline_lag_seconds else 'no - baseline captured too late'}`",
            f"- Baseline spot: `{baseline.spot_916:.2f}`",
            f"- Baseline ATM strike: `{baseline.strike:.0f}`",
            f"- Baseline CE / PE: `{format_money(baseline.ce_916)}` / `{format_money(baseline.pe_916)}`",
            f"- Baseline combined premium: `{baseline.combined_916:.2f}`",
            f"- 0.5% of baseline spot: `{threshold:.2f}`",
        ])

    lines.extend(["", "## 9:16 Paper Decay Tracker"])
    if baseline_tracker is None:
        lines.append("- Not available yet. This begins once the 9:16 baseline candle is available.")
    else:
        direction = "decayed" if baseline_tracker["decay_points"] > 0 else "expanded"
        lines.extend([
            f"- Current CE / PE at baseline strike: `{baseline_tracker['ce_now']:.2f}` / `{baseline_tracker['pe_now']:.2f}`",
            f"- Current combined premium: `{baseline_tracker['combined_now']:.2f}`",
            f"- Premium has `{direction}` by `{abs(baseline_tracker['decay_points']):.2f}` points versus baseline.",
            f"- Paper P&L from a baseline reference short straddle: `₹{baseline_tracker['paper_pnl_rupees_from_916']:.2f}`",
        ])

    lines.extend(["", "## Active Risk Monitor"])
    if risk_status is None:
        lines.append("- Risk monitor has not evaluated yet.")
    else:
        lines.extend([
            f"- Action: `{risk_status.get('action')}`",
            f"- Message: {risk_status.get('message')}",
            f"- Nearest stop: `{risk_status.get('nearest_stop_side', '-')}` distance `{format_money(risk_status.get('nearest_stop_distance'))}`",
            f"- Loss used / remaining: `₹{format_money(risk_status.get('loss_used'))}` / `₹{format_money(risk_status.get('loss_remaining'))}`",
            f"- Max-loss combined premium: `{format_money(risk_status.get('max_loss_combined'))}`",
        ])
    if alert_status:
        lines.extend([
            f"- Telegram alerts: `{alert_status.get('telegram')}`",
            f"- Alerts sent: `{len(alert_status.get('sent', []))}`",
        ])
        if alert_status.get("last_error"):
            lines.append(f"- Last alert error: `{alert_status['last_error']}`")

    lines.extend(["", "## Position"])
    if not position:
        lines.append("- No paper position is open.")
    else:
        lines.extend([
            f"- Status: `{position.status}`",
            f"- Strike: `{position.strike:.0f}`",
            f"- Entry time: `{position.entry_time}`",
            f"- Entry CE / PE: `{position.entry_ce:.2f}` / `{position.entry_pe:.2f}`",
            f"- Entry combined: `{position.entry_combined:.2f}`",
            f"- Spot stop band: `{position.stop_low:.2f}` to `{position.stop_high:.2f}`",
            f"- Max-loss circuit breaker: `₹{position.max_loss_rupees:.2f}`",
            f"- Last spot: `{format_money(position.last_spot)}`",
            f"- Last combined premium: `{format_money(position.last_combined)}`",
            f"- P&L points: `{format_money(position.pnl_points)}`",
            f"- P&L rupees: `{format_money(position.pnl_rupees)}`",
        ])
        if position.exit_reason:
            lines.append(f"- Exit reason: `{position.exit_reason}`")
        if position.exit_time:
            lines.append(f"- Exit time: `{position.exit_time}`")
    return "\n".join(lines) + "\n"


def load_todays_baseline_and_position(state_file: Path, session_date: date) -> tuple[BaselineSnapshot | None, PositionState | None]:
    state = read_json(state_file)
    if not state:
        return None, None

    baseline_payload = state.get("baseline")
    position_payload = state.get("position")
    baseline = None
    position = None

    baseline_source = str(baseline_payload.get("source", "")) if baseline_payload else ""
    if (
        baseline_payload
        and str(baseline_payload.get("captured_at", "")).startswith(session_date.isoformat())
        and baseline_source in {"live_quote_snapshot", "manual_user_input"}
    ):
        baseline_payload.setdefault("baseline_lag_seconds", 0.0)
        baseline = BaselineSnapshot(**baseline_payload)

    if position_payload and str(position_payload.get("entry_time", "")).startswith(session_date.isoformat()):
        if "max_loss_rupees" not in position_payload:
            position_payload["max_loss_rupees"] = 2000.0
        position = PositionState(**position_payload)

    return baseline, position


def main() -> int:
    args = parse_args()
    baseline_capture_grace = (
        args.baseline_live_fallback_grace_minutes
        if args.baseline_live_fallback_grace_minutes is not None
        else args.baseline_live_capture_grace_minutes
    )
    env_path = Path(args.env_file)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    latest_report = output_dir / "sensex_expiry_report.md"
    state_file = output_dir / "sensex_expiry_state.json"
    today = date.today()
    loss_alert_levels = parse_float_list(args.loss_alert_levels)
    spot_alert_distances = sorted(parse_float_list(args.spot_alert_distances), reverse=True)
    profit_alert_levels = parse_float_list(args.profit_alert_levels)
    sent_alerts = load_sent_alerts(state_file, today)
    last_alert_error: str | None = None

    env_text = read_env(env_path)
    refresh_ts = load_refresh_timestamp(env_text, args.account)
    telegram_token, telegram_chat_id = telegram_credentials(env_text)
    telegram_enabled = (
        not args.disable_telegram_alerts
        and bool(telegram_token)
        and bool(telegram_chat_id)
    )
    alert_status = {
        "session_date": today.isoformat(),
        "telegram": "enabled" if telegram_enabled else "disabled_or_missing_env",
        "sent": sorted(sent_alerts),
        "last_error": None,
        "active_risk_poll_seconds": args.active_risk_poll_seconds,
    }

    token_ok = False
    token_message = "Not checked yet."
    client: UpstoxClient | None = None
    try:
        token = load_access_token(env_text, args.account)
        candidate = UpstoxClient(token)
        quote = candidate.ensure_connection()
        client = candidate
        token_ok = True
        token_message = f"API connection is live for {args.account}."
    except Exception as exc:
        client = None
        token_message = (
            f"{args.account} token is not live right now: {exc}. "
            + (f"Last refresh timestamp in .env: {refresh_ts}." if refresh_ts else "No refresh timestamp found in .env.")
        )

    if client is None:
        preopen = {
            "pivot_levels": {
                "prev_close": 0.0,
                "pivot": 0.0,
                "bc": 0.0,
                "tc": 0.0,
                "r1": 0.0,
                "s1": 0.0,
                "r2": 0.0,
                "s2": 0.0,
                "cpr_width": 0.0,
                "cpr_width_pct": 0.0,
            },
            "spot": 0.0,
            "expiry": "-",
            "atm_strike_now": 0.0,
            "atm_combined_now": 0.0,
            "premium_threshold": 0.0,
            "threshold_satisfied_now": False,
            "nearby_straddles": [],
        }
        latest_report.write_text(render_report(args.account, token_ok, token_message, preopen, None, None, None, args.max_baseline_lag_seconds))
        write_json(state_file, {"account": args.account, "connection_live": False, "message": token_message})
        print(token_message)
        return 1

    # Pre-open / structural analysis using the live client.
    current_quote = client.quote(INDEX_KEY)
    contracts = client.option_contracts(INDEX_KEY)
    expiry = nearest_expiry(contracts, today)
    chain = client.option_chain(INDEX_KEY, expiry)

    daily_candles = client.candles(INDEX_KEY, "days", "1", today - timedelta(days=7), today)
    completed_days = [c for c in daily_candles if c.dt.date() < today]
    if not completed_days:
        raise RuntimeError("Could not find a completed SENSEX daily candle for pivot/CPR analysis.")
    prev_day = completed_days[-1]
    pivots = compute_pivots(prev_day)
    preopen = build_preopen_analysis(current_quote, pivots, expiry, chain, args.premium_threshold_pct)

    manual_baseline = build_manual_baseline(args, contracts, expiry, today)
    saved_baseline, saved_position = load_todays_baseline_and_position(state_file, today)
    if args.reset_position_state:
        saved_position = None
    baseline_time = parse_hhmm(args.baseline_time)
    baseline = manual_baseline or saved_baseline
    if baseline is None:
        baseline = capture_live_baseline_snapshot(
            quote=current_quote,
            chain=chain,
            contracts=contracts,
            expiry=expiry,
            session_date=today,
            baseline_time=baseline_time,
            grace_minutes=baseline_capture_grace,
        )
    position = saved_position
    baseline_tracker = build_baseline_decay_tracker(baseline, chain, args.lots)
    risk_status = compute_risk_status(position, float(current_quote["last_price"]) if position else None)

    latest_report.write_text(render_report(
        args.account,
        token_ok,
        token_message,
        preopen,
        baseline,
        baseline_tracker,
        position,
        args.max_baseline_lag_seconds,
        risk_status,
        alert_status,
    ))
    write_json(state_file, {
        "account": args.account,
        "connection_live": token_ok,
        "message": token_message,
        "risk": {
            "lots": args.lots,
            "max_loss_rupees": args.max_loss_rupees,
            "spot_stop_points": args.spot_stop_points,
            "premium_threshold_pct": args.premium_threshold_pct,
            "max_baseline_lag_seconds": args.max_baseline_lag_seconds,
        },
        "preopen": preopen,
        "baseline": asdict(baseline) if baseline else None,
        "baseline_tracker": baseline_tracker,
        "position": asdict(position) if position else None,
        "risk_status": risk_status,
        "alerts": alert_status,
    })

    if args.analysis_only:
        print(f"Wrote analysis report to {latest_report}")
        return 0

    eval_time = parse_hhmm(args.entry_eval_time)
    force_exit_time = parse_hhmm(args.force_exit_time)

    while True:
        current_quote = client.quote(INDEX_KEY)
        spot = float(current_quote["last_price"])
        chain = client.option_chain(INDEX_KEY, expiry)
        preopen = build_preopen_analysis(current_quote, pivots, expiry, chain, args.premium_threshold_pct)

        if baseline is None:
            baseline = capture_live_baseline_snapshot(
                quote=current_quote,
                chain=chain,
                contracts=contracts,
                expiry=expiry,
                session_date=today,
                baseline_time=baseline_time,
                grace_minutes=baseline_capture_grace,
            )
        baseline_tracker = build_baseline_decay_tracker(baseline, chain, args.lots)

        if baseline and position is None:
            position = maybe_enter_position(
                baseline=baseline,
                chain=chain,
                eval_time=eval_time,
                threshold_pct=args.premium_threshold_pct,
                stop_points=args.spot_stop_points,
                lots=args.lots,
                max_loss_rupees=args.max_loss_rupees,
                max_baseline_lag_seconds=args.max_baseline_lag_seconds,
            )

        if position is not None and position.status == "OPEN":
            position = update_position(position, spot, chain, force_exit_time)

        risk_status = compute_risk_status(position, spot if position else None)
        alert_keys, alert_message = build_alert_batch(
            position=position,
            risk_status=risk_status,
            loss_levels=loss_alert_levels,
            spot_distances=spot_alert_distances,
            profit_levels=profit_alert_levels,
            sent_alerts=sent_alerts,
        )
        if alert_keys and alert_message:
            if telegram_enabled and telegram_token and telegram_chat_id:
                last_alert_error = send_telegram_alert(telegram_token, telegram_chat_id, alert_message)
            else:
                last_alert_error = "Telegram alerts disabled or TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID missing."
            sent_alerts.update(alert_keys)

        alert_status = {
            "session_date": today.isoformat(),
            "telegram": "enabled" if telegram_enabled else "disabled_or_missing_env",
            "sent": sorted(sent_alerts),
            "last_error": last_alert_error,
            "active_risk_poll_seconds": args.active_risk_poll_seconds,
            "last_alert_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S") if alert_keys else None,
        }

        latest_report.write_text(render_report(
            args.account,
            token_ok,
            token_message,
            preopen,
            baseline,
            baseline_tracker,
            position,
            args.max_baseline_lag_seconds,
            risk_status,
            alert_status,
        ))
        write_json(state_file, {
            "account": args.account,
            "connection_live": token_ok,
            "message": token_message,
            "risk": {
                "lots": args.lots,
                "max_loss_rupees": args.max_loss_rupees,
                "spot_stop_points": args.spot_stop_points,
                "premium_threshold_pct": args.premium_threshold_pct,
                "max_baseline_lag_seconds": args.max_baseline_lag_seconds,
            },
            "preopen": preopen,
            "baseline": asdict(baseline) if baseline else None,
            "baseline_tracker": baseline_tracker,
            "position": asdict(position) if position else None,
            "risk_status": risk_status,
            "alerts": alert_status,
        })

        if position is not None and position.status == "CLOSED":
            print(f"Position closed: {position.exit_reason} | P&L ₹{position.pnl_rupees:.2f}")
            return 0

        sleep_seconds = args.pre_entry_poll_seconds if position is None else min(args.poll_seconds, args.active_risk_poll_seconds)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
