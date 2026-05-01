#!/usr/bin/env python3.11
from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

REPO_ROOT = Path(__file__).resolve().parents[4]
LEGACY_OUTPUT_ROOT = REPO_ROOT / "data" / "legacy-analyzers"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"


ROOTS = ["GOLDM", "SILVERMIC", "CRUDEOILM", "ZINCMINI", "NATGASMINI"]
CONTRACT_MULTIPLIERS = {
    # Rupee P&L multiplier for a 1-point move in the quoted futures price.
    "GOLDM": 10,
    "SILVERMIC": 1,
    "CRUDEOILM": 10,
    "ZINCMINI": 1000,
    "NATGASMINI": 250,
}
INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz"
QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
ENV_KEYS = ("UPSTOX_ACCESS_TOKEN", "ACCESS_TOKEN", "UPSTOX_TOKEN")
HISTORICAL_URL = "https://api.upstox.com/v3/historical-candle"


@dataclass
class Candle:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class CommoditySnapshot:
    root: str
    trading_symbol: str
    instrument_key: str
    lot_size: int
    ltp: float
    open_price: float
    day_high: float
    day_low: float
    prev_close: float
    average_price: float
    volume: float
    oi: float
    net_change: float
    score: int
    bias: str
    note: str
    stop_loss: float | None = None
    stop_basis: str = "-"
    target_1: float | None = None
    target_2: float | None = None
    trail_fast: float | None = None
    rr_target: float | None = None
    next_key_level: float | None = None
    target_within_day_range: bool | None = None
    entry_trigger: float | None = None
    risk_per_lot: float | None = None
    risk_two_lots: float | None = None
    actionable: bool = False
    actionable_reason: str = "-"
    allowed_lots: int = 0
    status: str = "Skip"

    @property
    def live_vs_prev_close_pct(self) -> float:
        return ((self.ltp - self.prev_close) / self.prev_close) * 100 if self.prev_close else 0.0

    @property
    def live_vs_open_pct(self) -> float:
        return ((self.ltp - self.open_price) / self.open_price) * 100 if self.open_price else 0.0

    @property
    def distance_from_high_pct(self) -> float:
        return ((self.day_high - self.ltp) / self.day_high) * 100 if self.day_high else 0.0

    @property
    def distance_from_low_pct(self) -> float:
        return ((self.ltp - self.day_low) / self.day_low) * 100 if self.day_low else 0.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor MCX commodities using Upstox live quotes.")
    parser.add_argument(
        "--env-file",
        default=str(DEFAULT_ENV_FILE),
        help="Path to the .env file containing the Upstox access token.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(LEGACY_OUTPUT_ROOT / "mcx-monitor"),
        help="Directory where reports and state files will be written.",
    )
    parser.add_argument(
        "--roots",
        nargs="*",
        default=ROOTS,
        help="Commodity roots to track.",
    )
    parser.add_argument(
        "--max-risk-rupees",
        type=float,
        default=2000.0,
        help="Maximum acceptable rupee risk per trade.",
    )
    return parser.parse_args()


def load_access_token(env_file: Path) -> str:
    text = env_file.read_text()
    for key in ENV_KEYS:
        match = re.search(rf"^{key}=(.+)$", text, re.M)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    raise RuntimeError(f"Could not find any of {ENV_KEYS} in {env_file}")


def fetch_instruments() -> list[dict[str, Any]]:
    response = requests.get(INSTRUMENT_URL, timeout=30)
    response.raise_for_status()
    return json.loads(gzip.decompress(response.content))


def nearest_contracts(instruments: list[dict[str, Any]], roots: list[str]) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for root in roots:
        matches = [
            row
            for row in instruments
            if row.get("segment") == "MCX_FO"
            and row.get("instrument_type") == "FUT"
            and row.get("underlying_symbol") == root
        ]
        matches.sort(key=lambda row: row.get("expiry"))
        if matches:
            selected.append(matches[0])
    return selected


def fetch_quotes(token: str, instrument_keys: list[str]) -> dict[str, Any]:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    response = requests.get(
        QUOTE_URL,
        headers=headers,
        params={"instrument_key": ",".join(instrument_keys)},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    return payload.get("data", {})


def fetch_historical_candles(token: str, instrument_key: str, unit: str, interval: str, days_back: int) -> list[Candle]:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=days_back)
    encoded = quote(instrument_key, safe="")
    url = f"{HISTORICAL_URL}/{encoded}/{unit}/{interval}/{end_date.isoformat()}/{start_date.isoformat()}"
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    payload = response.json()
    rows = payload.get("data", {}).get("candles", [])
    candles = [
        Candle(
            ts=row[0],
            open=float(row[1]),
            high=float(row[2]),
            low=float(row[3]),
            close=float(row[4]),
            volume=float(row[5]),
        )
        for row in rows
    ]
    candles.sort(key=lambda row: row.ts)
    return candles


def find_quote_for_contract(quotes: dict[str, Any], instrument_key: str, trading_symbol: str) -> dict[str, Any] | None:
    direct = quotes.get(instrument_key)
    if direct:
        return direct

    alt = quotes.get(instrument_key.replace("|", ":"))
    if alt:
        return alt

    compact_symbol = trading_symbol.replace(" FUT ", "").replace(" ", "")
    for map_key, value in quotes.items():
        if map_key.endswith(compact_symbol):
            return value
        if value.get("instrument_token") == instrument_key:
            return value
    return None


def score_snapshot(root: str, quote: dict[str, Any], contract: dict[str, Any]) -> CommoditySnapshot:
    ohlc = quote.get("ohlc", {})
    ltp = float(quote["last_price"])
    open_price = float(ohlc["open"])
    day_high = float(ohlc["high"])
    day_low = float(ohlc["low"])
    average_price = float(quote.get("average_price") or 0.0)
    volume = float(quote.get("volume") or 0.0)
    oi = float(quote.get("oi") or 0.0)
    net_change = float(quote.get("net_change") or 0.0)
    # Upstox quote `ohlc.close` tracks the current last price for MCX here.
    # Previous close is therefore inferred from the absolute net change.
    prev_close = ltp - net_change if net_change else float(ohlc.get("close") or ltp)

    score = 0
    notes: list[str] = []

    if ltp > prev_close:
        score += 1
        notes.append("above previous close")
    elif ltp < prev_close:
        score -= 1
        notes.append("below previous close")

    if ltp > open_price:
        score += 1
        notes.append("above open")
    elif ltp < open_price:
        score -= 1
        notes.append("below open")

    if average_price:
        if ltp > average_price:
            score += 1
            notes.append("above VWAP-like average")
        elif ltp < average_price:
            score -= 1
            notes.append("below VWAP-like average")

    range_size = day_high - day_low
    upper_break_zone = day_high - (range_size * 0.15) if range_size else day_high
    lower_break_zone = day_low + (range_size * 0.15) if range_size else day_low

    if ltp >= upper_break_zone:
        score += 1
        notes.append("near day high")
    elif ltp <= lower_break_zone:
        score -= 1
        notes.append("near day low")

    if net_change > 0:
        score += 1
    elif net_change < 0:
        score -= 1

    if score >= 3:
        bias = "bullish"
    elif score <= -3:
        bias = "bearish"
    else:
        bias = "neutral"

    return CommoditySnapshot(
        root=root,
        trading_symbol=contract["trading_symbol"],
        instrument_key=contract["instrument_key"],
        lot_size=int(CONTRACT_MULTIPLIERS.get(root) or contract.get("lot_size") or 0),
        ltp=ltp,
        open_price=open_price,
        day_high=day_high,
        day_low=day_low,
        prev_close=prev_close,
        average_price=average_price,
        volume=volume,
        oi=oi,
        net_change=net_change,
        score=score,
        bias=bias,
        note=", ".join(notes),
    )


def atr(candles: list[Candle], period: int) -> list[float | None]:
    values: list[float | None] = []
    trs: list[float] = []
    prev_close: float | None = None
    for candle in candles:
        if prev_close is None:
            tr = candle.high - candle.low
        else:
            tr = max(candle.high - candle.low, abs(candle.high - prev_close), abs(candle.low - prev_close))
        trs.append(tr)
        if len(trs) < period:
            values.append(None)
        elif len(trs) == period:
            values.append(sum(trs) / period)
        else:
            prev_atr = values[-1] if values[-1] is not None else sum(trs[-period:]) / period
            values.append(((prev_atr * (period - 1)) + tr) / period)
        prev_close = candle.close
    return values


def supertrend(candles: list[Candle], period: int, multiplier: float) -> list[float | None]:
    atr_values = atr(candles, period)
    upper_bands: list[float | None] = []
    lower_bands: list[float | None] = []
    final_upper: list[float | None] = []
    final_lower: list[float | None] = []
    trends: list[bool] = []
    lines: list[float | None] = []

    for idx, candle in enumerate(candles):
        a = atr_values[idx]
        if a is None:
            upper_bands.append(None)
            lower_bands.append(None)
            final_upper.append(None)
            final_lower.append(None)
            trends.append(True)
            lines.append(None)
            continue

        hl2 = (candle.high + candle.low) / 2
        basic_upper = hl2 + (multiplier * a)
        basic_lower = hl2 - (multiplier * a)
        upper_bands.append(basic_upper)
        lower_bands.append(basic_lower)

        if idx == 0 or final_upper[-1] is None or final_lower[-1] is None:
            curr_final_upper = basic_upper
            curr_final_lower = basic_lower
            curr_trend = candle.close >= basic_lower
        else:
            prev_final_upper = final_upper[-1]
            prev_final_lower = final_lower[-1]
            prev_close = candles[idx - 1].close
            curr_final_upper = basic_upper if basic_upper < prev_final_upper or prev_close > prev_final_upper else prev_final_upper
            curr_final_lower = basic_lower if basic_lower > prev_final_lower or prev_close < prev_final_lower else prev_final_lower

            prev_trend = trends[-1]
            if prev_trend:
                curr_trend = candle.close >= curr_final_lower
            else:
                curr_trend = candle.close > curr_final_upper

        final_upper.append(curr_final_upper)
        final_lower.append(curr_final_lower)
        trends.append(curr_trend)
        lines.append(curr_final_lower if curr_trend else curr_final_upper)

    return lines


def find_recent_swing(candles: list[Candle], direction: str, lookback: int = 6) -> float | None:
    if len(candles) < 3:
        return None
    window = candles[-(lookback + 2):-1] if len(candles) > lookback + 1 else candles[:-1]
    if not window:
        return None
    if direction == "Long":
        return min(candle.low for candle in window)
    return max(candle.high for candle in window)


def find_micro_swing(candles: list[Candle], direction: str, bars: int = 3) -> float | None:
    if len(candles) < bars + 1:
        return None
    window = candles[-(bars + 1):-1]
    if direction == "Long":
        return min(candle.low for candle in window)
    return max(candle.high for candle in window)


def previous_pivot_levels(daily_candles: list[Candle]) -> tuple[float | None, float | None, float | None]:
    if not daily_candles:
        return None, None, None
    prev = daily_candles[-1]
    pivot = (prev.high + prev.low + prev.close) / 3
    r1 = (2 * pivot) - prev.low
    s1 = (2 * pivot) - prev.high
    return pivot, r1, s1


def enrich_trade_levels(snapshot: CommoditySnapshot, candles_15m: list[Candle], daily_candles: list[Candle], max_risk_rupees: float) -> None:
    if snapshot.bias not in {"bullish", "bearish"} or len(candles_15m) < 10:
        return

    direction = "Long" if snapshot.bias == "bullish" else "Short"
    st_slow = supertrend(candles_15m, 5, 3.0)
    st_fast = supertrend(candles_15m, 5, 1.5)
    slow_value = next((value for value in reversed(st_slow) if value is not None), None)
    fast_value = next((value for value in reversed(st_fast) if value is not None), None)
    swing = find_recent_swing(candles_15m, direction)
    micro_swing = find_micro_swing(candles_15m, direction)
    pivot, r1, s1 = previous_pivot_levels(daily_candles)

    if direction == "Long":
        entry_trigger = snapshot.day_high if snapshot.ltp < snapshot.day_high else snapshot.ltp
        candidate_stops = [value for value in (micro_swing, swing, slow_value) if value is not None and value < snapshot.ltp]
        stop = max(candidate_stops) if candidate_stops else None
        if stop is not None and micro_swing is not None and abs(stop - micro_swing) < 1e-9:
            stop_basis = "micro 15m swing low"
        elif stop is not None and swing is not None and abs(stop - swing) < 1e-9:
            stop_basis = "previous swing low"
        elif stop is not None and slow_value is not None:
            stop_basis = "ST(5,3)"
        else:
            stop_basis = "-"
        next_key_candidates = [level for level in (r1, pivot) if level is not None and level > entry_trigger]
        next_key_level = min(next_key_candidates) if next_key_candidates else None
        if stop is not None:
            rr = entry_trigger + (entry_trigger - stop)
            target_1 = min(rr, next_key_level) if next_key_level else rr
            target_within = target_1 <= snapshot.day_high if target_1 is not None else None
            target_2 = next_key_level or rr
        else:
            rr = target_1 = target_2 = None
            target_within = None
    else:
        entry_trigger = snapshot.day_low if snapshot.ltp > snapshot.day_low else snapshot.ltp
        candidate_stops = [value for value in (micro_swing, swing, slow_value) if value is not None and value > snapshot.ltp]
        stop = min(candidate_stops) if candidate_stops else None
        if stop is not None and micro_swing is not None and abs(stop - micro_swing) < 1e-9:
            stop_basis = "micro 15m swing high"
        elif stop is not None and swing is not None and abs(stop - swing) < 1e-9:
            stop_basis = "previous swing high"
        elif stop is not None and slow_value is not None:
            stop_basis = "ST(5,3)"
        else:
            stop_basis = "-"
        next_key_candidates = [level for level in (s1, pivot) if level is not None and level < entry_trigger]
        next_key_level = max(next_key_candidates) if next_key_candidates else None
        if stop is not None:
            rr = entry_trigger - (stop - entry_trigger)
            target_1 = max(rr, next_key_level) if next_key_level else rr
            target_within = target_1 >= snapshot.day_low if target_1 is not None else None
            target_2 = next_key_level or rr
        else:
            rr = target_1 = target_2 = None
            target_within = None

    snapshot.entry_trigger = entry_trigger
    snapshot.stop_loss = stop
    snapshot.stop_basis = stop_basis
    snapshot.target_1 = target_1
    snapshot.target_2 = target_2
    snapshot.trail_fast = fast_value
    snapshot.rr_target = rr
    snapshot.next_key_level = next_key_level
    snapshot.target_within_day_range = target_within
    if stop is not None and entry_trigger is not None and snapshot.lot_size:
        point_risk = abs(entry_trigger - stop)
        snapshot.risk_per_lot = point_risk * snapshot.lot_size
        snapshot.risk_two_lots = snapshot.risk_per_lot * 2
        snapshot.allowed_lots = int(max_risk_rupees // snapshot.risk_per_lot) if snapshot.risk_per_lot > 0 else 0
        within_budget = snapshot.risk_two_lots <= max_risk_rupees
        target_is_practical = snapshot.target_within_day_range is True
        snapshot.actionable = within_budget and target_is_practical
        reasons = []
        if not within_budget:
            reasons.append(f"2-lot risk {snapshot.risk_two_lots:.0f} exceeds cap {max_risk_rupees:.0f}")
        if not target_is_practical:
            reasons.append("T1 is outside current day range")
        snapshot.actionable_reason = "actionable" if snapshot.actionable else "; ".join(reasons) if reasons else "-"
        if snapshot.actionable:
            snapshot.status = "Trade"
        elif snapshot.allowed_lots >= 1 and target_is_practical:
            snapshot.status = "Reduce size"
        else:
            snapshot.status = "Skip"


def trade_plan(snapshot: CommoditySnapshot) -> dict[str, str]:
    if snapshot.bias == "bullish":
        trigger = snapshot.entry_trigger if snapshot.entry_trigger is not None else snapshot.day_high
        entry = f"above {trigger:.2f}" if snapshot.ltp < snapshot.day_high else f"pullback hold above {snapshot.open_price:.2f}"
        direction = "Long"
    elif snapshot.bias == "bearish":
        trigger = snapshot.entry_trigger if snapshot.entry_trigger is not None else snapshot.day_low
        entry = f"below {trigger:.2f}" if snapshot.ltp > snapshot.day_low else f"weak bounce fail below {snapshot.open_price:.2f}"
        direction = "Short"
    else:
        entry = "Wait"
        direction = "Skip"

    stop = f"{snapshot.stop_loss:.2f}" if snapshot.stop_loss is not None else "-"
    target_1 = f"{snapshot.target_1:.2f}" if snapshot.target_1 is not None else "-"
    target_2 = f"{snapshot.target_2:.2f}" if snapshot.target_2 is not None else "-"
    trail = f"{snapshot.trail_fast:.2f}" if snapshot.trail_fast is not None else "-"
    rr = f"{snapshot.rr_target:.2f}" if snapshot.rr_target is not None else "-"
    key_level = f"{snapshot.next_key_level:.2f}" if snapshot.next_key_level is not None else "-"
    two_lot = "2 lots: book 1 at T1, trail 1 with ST(5,1.5)"
    within = (
        "yes"
        if snapshot.target_within_day_range is True
        else "no"
        if snapshot.target_within_day_range is False
        else "-"
    )
    risk_one = f"{snapshot.risk_per_lot:.0f}" if snapshot.risk_per_lot is not None else "-"
    risk_two = f"{snapshot.risk_two_lots:.0f}" if snapshot.risk_two_lots is not None else "-"
    actionable = "yes" if snapshot.actionable else "no"
    allowed_lots = str(snapshot.allowed_lots)
    return {
        "direction": direction,
        "entry": entry,
        "stop": stop,
        "stop_basis": snapshot.stop_basis,
        "target_1": target_1,
        "target_2": target_2,
        "trail_fast": trail,
        "rr_target": rr,
        "next_key_level": key_level,
        "within_day_range": within,
        "management": two_lot if direction != "Skip" else "-",
        "risk_one_lot": risk_one,
        "risk_two_lots": risk_two,
        "actionable": actionable,
        "actionable_reason": snapshot.actionable_reason,
        "allowed_lots": allowed_lots,
        "status": snapshot.status,
    }


def load_previous_state(state_file: Path) -> dict[str, Any]:
    if not state_file.exists():
        return {}
    return json.loads(state_file.read_text())


def save_state(state_file: Path, snapshots: list[CommoditySnapshot]) -> None:
    payload = {
        "generated_at": datetime.now().isoformat(),
        "commodities": {
            item.root: {
                "ltp": item.ltp,
                "score": item.score,
                "bias": item.bias,
            }
            for item in snapshots
        },
    }
    state_file.write_text(json.dumps(payload, indent=2))


def trend_change(snapshot: CommoditySnapshot, previous_state: dict[str, Any]) -> str:
    prev = previous_state.get("commodities", {}).get(snapshot.root)
    if not prev:
        return "first run"
    prev_score = prev.get("score", 0)
    prev_bias = prev.get("bias", "unknown")
    if snapshot.score > prev_score:
        return f"strengthening vs previous run ({prev_bias} -> {snapshot.bias})"
    if snapshot.score < prev_score:
        return f"weakening vs previous run ({prev_bias} -> {snapshot.bias})"
    return f"unchanged vs previous run ({snapshot.bias})"


def write_report(report_file: Path, snapshots: list[CommoditySnapshot], previous_state: dict[str, Any]) -> None:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    strongest_long = max(snapshots, key=lambda item: item.score)
    strongest_short = min(snapshots, key=lambda item: item.score)
    lines = [
        f"# MCX Commodity Monitor",
        "",
        f"Generated: {now}",
        "",
        f"- Strongest long bias: `{strongest_long.root}` (score `{strongest_long.score}`)",
        f"- Strongest short bias: `{strongest_short.root}` (score `{strongest_short.score}`)",
        "",
        "| Commodity | Bias | Score | Lot | LTP | Prev Close | Open | High | Low | Status | Thesis Change | Entry | SL | T1 | T2 / Trail | Risk | Notes |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---|---|---|---|---|---|---|---|",
    ]
    for item in sorted(snapshots, key=lambda row: row.score, reverse=True):
        plan = trade_plan(item)
        change = trend_change(item, previous_state)
        notes = (
            f"{plan['direction']}; {plan['management']}; "
            f"SL basis: {plan['stop_basis']}; "
            f"1:1={plan['rr_target']}; next key={plan['next_key_level']}; "
            f"T1 within day range={plan['within_day_range']}; "
            f"allowed lots={plan['allowed_lots']}; "
            f"actionable={plan['actionable']} ({plan['actionable_reason']})"
        )
        t2_trail = f"T2 {plan['target_2']} / Trail {plan['trail_fast']}"
        risk = f"1L {plan['risk_one_lot']} / 2L {plan['risk_two_lots']}"
        lines.append(
            f"| `{item.root}` | `{item.bias}` | `{item.score}` | `{item.lot_size}` | `{item.ltp:.2f}` | `{item.prev_close:.2f}` | `{item.open_price:.2f}` | `{item.day_high:.2f}` | `{item.day_low:.2f}` | `{plan['status']}` | {change} | {plan['entry']} | `{plan['stop']}` | `{plan['target_1']}` | {t2_trail} | {risk} | {notes} |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- Score uses previous close, open, average traded price, range location, and net change.",
            "- For MCX live quotes, previous close is inferred as `last_price - net_change` because Upstox `ohlc.close` can equal the current last price.",
            "- `strengthening` means the current run's score is stronger than the last saved run.",
            "- Initial stop uses the more conservative of previous 15m swing and `Supertrend(5,3)` when available.",
            "- The script prefers the tighter valid intraday stop by checking a micro 15m swing before wider swing / supertrend levels.",
            "- Trade management assumes 2 lots: book 1 lot at the initial target and trail the remaining lot with `Supertrend(5,1.5)`.",
            "- Risk is computed using the actual futures lot size. A setup is marked actionable only if the 2-lot rupee risk is within your configured cap and T1 is still realistic inside today's range.",
            "- `Status` means: `Trade` = usable as planned, `Reduce size` = setup is valid but only with fewer lots, `Skip` = do not take it under the current rules.",
            "- `T1 within day range` tells you whether the first target is still inside the current day's high-low envelope.",
            "- Use this as a screening report, not a blind execution engine.",
        ]
    )
    report_file.write_text("\n".join(lines))


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_file = output_dir / "latest_report.md"
    state_file = output_dir / "state.json"

    token = load_access_token(env_file)
    instruments = fetch_instruments()
    contracts = nearest_contracts(instruments, args.roots)
    quotes = fetch_quotes(token, [row["instrument_key"] for row in contracts])

    snapshots: list[CommoditySnapshot] = []
    for contract in contracts:
        key = contract["instrument_key"]
        quote = find_quote_for_contract(quotes, key, contract["trading_symbol"])
        if not quote:
            raise RuntimeError(f"No live quote returned for {key}")
        snapshot = score_snapshot(contract["underlying_symbol"], quote, contract)
        intraday_candles = fetch_historical_candles(token, key, "minutes", "15", days_back=3)
        daily_candles = fetch_historical_candles(token, key, "days", "1", days_back=7)
        enrich_trade_levels(snapshot, intraday_candles, daily_candles, args.max_risk_rupees)
        snapshots.append(snapshot)

    previous_state = load_previous_state(state_file)
    write_report(report_file, snapshots, previous_state)
    save_state(state_file, snapshots)

    print(f"Wrote report to {report_file}")
    print(f"Wrote state to {state_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
