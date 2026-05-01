#!/usr/bin/env python3.11
from __future__ import annotations

import argparse
import gzip
import json
import math
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

REPO_ROOT = Path(__file__).resolve().parents[4]
LEGACY_ROOT = REPO_ROOT / "apps" / "analyzers-upstox" / "legacy"
LEGACY_OUTPUT_ROOT = REPO_ROOT / "data" / "legacy-analyzers"
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_UNIVERSE_FILE = LEGACY_ROOT / "stock_fo_monitor" / "universe_nifty_fo.txt"


NSE_INSTRUMENT_URL = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
QUOTE_URL = "https://api.upstox.com/v2/market-quote/quotes"
HISTORICAL_URL = "https://api.upstox.com/v3/historical-candle"
ENV_KEYS = ("UPSTOX_ACCESS_TOKEN", "ACCESS_TOKEN", "UPSTOX_TOKEN")
INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "SENSEX": "BSE_INDEX|SENSEX",
    "INDIAVIX": "NSE_INDEX|India VIX",
}


@dataclass
class Candle:
    ts: str
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class IntradaySnapshot:
    symbol: str
    name: str
    instrument_key: str
    ltp: float
    open_price: float
    day_high: float
    day_low: float
    prev_close: float
    average_price: float
    volume: float
    pct_change: float
    rs_vs_nifty: float
    daily_ma20: float
    daily_ma50: float
    daily_ma100: float
    daily_rsi14: float | None
    daily_adx14: float | None
    plus_di: float | None
    minus_di: float | None
    st_slow: float | None
    st_fast: float | None
    pivot: float | None
    r1: float | None
    s1: float | None
    cpr_width_pct: float | None
    cpr_tag: str
    score: int
    bias: str
    note: str
    entry_trigger: float | None = None
    stop_loss: float | None = None
    stop_basis: str = "-"
    target_1: float | None = None
    target_2: float | None = None
    rr_one: float | None = None
    next_key_level: float | None = None
    trail_level: float | None = None
    actionable: bool = False
    action_reason: str = "-"
    option_note: str = "-"
    intraday_data_fresh: bool = True


@dataclass
class IndexTradePlan:
    symbol: str
    bias: str
    confidence: str
    setup: str
    trigger_after_30m: str
    invalidation: str
    management: str
    option_plan: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze intraday Nifty F&O stocks and indices using Upstox data.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--universe-file", default=str(DEFAULT_UNIVERSE_FILE))
    parser.add_argument("--output-dir", default=str(LEGACY_OUTPUT_ROOT / "stock-intraday"))
    parser.add_argument("--top", type=int, default=8)
    parser.add_argument("--narrow-cpr-threshold-pct", type=float, default=0.40)
    return parser.parse_args()


def load_access_token(env_file: Path) -> str:
    text = env_file.read_text()
    for key in ENV_KEYS:
        match = re.search(rf"^{key}=(.+)$", text, re.M)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    raise RuntimeError(f"Could not find any of {ENV_KEYS} in {env_file}")


def load_universe(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text().splitlines() if line.strip() and not line.strip().startswith("#")]


def fetch_nse_instruments() -> list[dict[str, Any]]:
    response = requests.get(NSE_INSTRUMENT_URL, timeout=30)
    response.raise_for_status()
    return json.loads(gzip.decompress(response.content))


def map_stock_instruments(instruments: list[dict[str, Any]], symbols: list[str]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    wanted = set(symbols)
    for row in instruments:
        if row.get("segment") != "NSE_EQ" or row.get("instrument_type") != "EQ":
            continue
        trading_symbol = row.get("trading_symbol")
        if trading_symbol in wanted:
            lookup[trading_symbol] = row
    missing = wanted - set(lookup)
    if missing:
        raise RuntimeError(f"Missing NSE cash instruments for: {', '.join(sorted(missing))}")
    return lookup


def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[idx: idx + size] for idx in range(0, len(items), size)]


def fetch_quotes(token: str, keys: list[str]) -> dict[str, Any]:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    merged: dict[str, Any] = {}
    for batch in chunked(keys, 50):
        response = requests.get(QUOTE_URL, headers=headers, params={"instrument_key": ",".join(batch)}, timeout=20)
        response.raise_for_status()
        merged.update(response.json().get("data", {}))
    return merged


def find_quote(quotes: dict[str, Any], instrument_key: str, trading_symbol: str) -> dict[str, Any] | None:
    direct = quotes.get(instrument_key)
    if direct:
        return direct
    alt = quotes.get(instrument_key.replace("|", ":"))
    if alt:
        return alt
    for map_key, value in quotes.items():
        if value.get("instrument_token") == instrument_key or map_key.endswith(trading_symbol):
            return value
    return None


def fetch_candles(token: str, instrument_key: str, unit: str, interval: str, days_back: int) -> list[Candle]:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    end_date = date.today()
    start_date = end_date - timedelta(days=days_back)
    encoded = quote(instrument_key, safe="")
    url = f"{HISTORICAL_URL}/{encoded}/{unit}/{interval}/{end_date.isoformat()}/{start_date.isoformat()}"
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    rows = response.json().get("data", {}).get("candles", [])
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
    candles.sort(key=lambda item: item.ts)
    return candles


def candle_date(candle: Candle) -> date | None:
    try:
        return datetime.fromisoformat(candle.ts).date()
    except Exception:
        return None


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


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
            values.append(sum(trs[-period:]) / period)
        else:
            prev_atr = values[-1] if values[-1] is not None else sum(trs[-period:]) / period
            values.append(((prev_atr * (period - 1)) + tr) / period)
        prev_close = candle.close
    return values


def supertrend(candles: list[Candle], period: int, multiplier: float) -> list[float | None]:
    atr_values = atr(candles, period)
    final_upper: list[float | None] = []
    final_lower: list[float | None] = []
    bull_trend: list[bool] = []
    out: list[float | None] = []
    for idx, candle in enumerate(candles):
        current_atr = atr_values[idx]
        if current_atr is None:
            final_upper.append(None)
            final_lower.append(None)
            bull_trend.append(True)
            out.append(None)
            continue
        hl2 = (candle.high + candle.low) / 2
        basic_upper = hl2 + multiplier * current_atr
        basic_lower = hl2 - multiplier * current_atr
        if idx == 0 or final_upper[-1] is None or final_lower[-1] is None:
            fu = basic_upper
            fl = basic_lower
            bull = candle.close >= basic_lower
        else:
            prev_close = candles[idx - 1].close
            prev_fu = final_upper[-1]
            prev_fl = final_lower[-1]
            fu = basic_upper if basic_upper < prev_fu or prev_close > prev_fu else prev_fu
            fl = basic_lower if basic_lower > prev_fl or prev_close < prev_fl else prev_fl
            bull = candle.close >= fl if bull_trend[-1] else candle.close > fu
        final_upper.append(fu)
        final_lower.append(fl)
        bull_trend.append(bull)
        out.append(fl if bull else fu)
    return out


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for idx in range(1, period + 1):
        delta = values[idx] - values[idx - 1]
        gains.append(max(delta, 0))
        losses.append(abs(min(delta, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for idx in range(period + 1, len(values)):
        delta = values[idx] - values[idx - 1]
        gain = max(delta, 0)
        loss = abs(min(delta, 0))
        avg_gain = ((avg_gain * (period - 1)) + gain) / period
        avg_loss = ((avg_loss * (period - 1)) + loss) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def adx(candles: list[Candle], period: int = 14) -> tuple[float | None, float | None, float | None]:
    if len(candles) <= period + 1:
        return None, None, None
    trs: list[float] = []
    plus_dm: list[float] = []
    minus_dm: list[float] = []
    for idx in range(1, len(candles)):
        current = candles[idx]
        previous = candles[idx - 1]
        up_move = current.high - previous.high
        down_move = previous.low - current.low
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0.0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0.0)
        trs.append(max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close)))

    atr14 = sum(trs[:period])
    plus14 = sum(plus_dm[:period])
    minus14 = sum(minus_dm[:period])
    dx_values = []
    for idx in range(period, len(trs)):
        if idx > period:
            atr14 = atr14 - (atr14 / period) + trs[idx]
            plus14 = plus14 - (plus14 / period) + plus_dm[idx]
            minus14 = minus14 - (minus14 / period) + minus_dm[idx]
        plus_di = 100 * (plus14 / atr14) if atr14 else 0.0
        minus_di = 100 * (minus14 / atr14) if atr14 else 0.0
        total = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / total if total else 0.0
        dx_values.append((dx, plus_di, minus_di))
    if not dx_values:
        return None, None, None
    if len(dx_values) < period:
        last = dx_values[-1]
        return last[0], last[1], last[2]
    adx_value = sum(item[0] for item in dx_values[:period]) / period
    for idx in range(period, len(dx_values)):
        adx_value = ((adx_value * (period - 1)) + dx_values[idx][0]) / period
    last_plus = dx_values[-1][1]
    last_minus = dx_values[-1][2]
    return adx_value, last_plus, last_minus


def compute_cpr(candle: Candle) -> tuple[float, float, float, float, float]:
    pivot = (candle.high + candle.low + candle.close) / 3.0
    bc = (candle.high + candle.low) / 2.0
    tc = (pivot * 2.0) - bc
    r1 = (pivot * 2.0) - candle.low
    s1 = (pivot * 2.0) - candle.high
    return pivot, bc, tc, r1, s1


def intraday_option_note(symbol: str, bias: str, cpr_tag: str, vix_regime: str, weekday: int) -> str:
    if symbol == "NIFTY":
        if weekday == 1:
            if bias == "bullish":
                return "Tuesday 0DTE Nifty: favor bullish 0DTE structures. High VIX -> bull put spread / broken-wing credit; lower VIX -> bull call spread."
            if bias == "bearish":
                return "Tuesday 0DTE Nifty: favor bearish 0DTE structures. High VIX -> bear call spread; lower VIX -> bear put spread."
            return "Tuesday 0DTE Nifty: stay selective unless price breaks cleanly from the opening range."
        return "Use Nifty as the primary intraday index read."
    if symbol == "SENSEX":
        if weekday == 3:
            return "Thursday 0DTE Sensex: use Nifty trend as the primary proxy and trade Sensex more selectively because it can move more aggressively."
        return "Sensex is monitored mainly as a secondary confirmation to Nifty."
    if symbol == "BANKNIFTY":
        return "Bank Nifty helps confirm risk-on / risk-off breadth and can validate index-trend conviction."
    return "-"


def index_option_plan(symbol: str, bias: str, vix_regime: str, weekday: int) -> str:
    if symbol == "NIFTY" and weekday == 1:
        if bias == "bullish":
            return "Tuesday 0DTE Nifty: use bullish 0DTE only after opening-range acceptance. High VIX -> bull put spread / broken-wing credit. Lower VIX -> bull call spread."
        if bias == "bearish":
            return "Tuesday 0DTE Nifty: use bearish 0DTE only after opening-range failure. High VIX -> bear call spread. Lower VIX -> bear put spread."
        return "Tuesday 0DTE Nifty: if bias is mixed, prefer no trade unless price stabilizes clearly outside the CPR and opening range."
    if symbol == "SENSEX" and weekday == 3:
        return "Thursday 0DTE Sensex: use Nifty as the main directional proxy. Prefer defined-risk spreads only after Nifty and Sensex confirm the same side."
    if bias == "bullish":
        return "Non-0DTE index swing: prefer bullish defined-risk structures only if price stabilizes above pivot / VWAP / fast MAs after the first 30 minutes."
    if bias == "bearish":
        return "Non-0DTE index swing: prefer bearish defined-risk structures only if price stabilizes below pivot / VWAP / fast MAs after the first 30 minutes."
    return "Mixed index day: wait for opening-range resolution before considering any intraday option structure."


def build_index_trade_plan(snapshot: IntradaySnapshot, weekday: int) -> IndexTradePlan:
    pivot = snapshot.pivot or snapshot.ltp
    r1 = snapshot.r1 or snapshot.day_high
    s1 = snapshot.s1 or snapshot.day_low
    if snapshot.bias == "bullish":
        confidence = "high" if snapshot.cpr_tag == "narrow" and snapshot.score >= 6 else "medium"
        setup = "Bullish continuation after 30-minute stabilization above pivot/VWAP"
        trigger = f"Wait 30 minutes. Act only if price holds above pivot {pivot:.2f} and reclaims/holds above the early range, ideally pushing toward {r1:.2f}."
        invalidation = f"Avoid or exit if price slips back below pivot {pivot:.2f} or loses the opening-range low after the trigger."
        management = f"Trail with 15m structure / ST(5,1.5). Partial around 1:1 or near R1 {r1:.2f}; keep runner only on a trend day."
    elif snapshot.bias == "bearish":
        confidence = "high" if snapshot.cpr_tag == "narrow" and snapshot.score <= -6 else "medium"
        setup = "Bearish continuation after 30-minute stabilization below pivot/VWAP"
        trigger = f"Wait 30 minutes. Act only if price stays below pivot {pivot:.2f} and opening-range support fails, ideally heading toward {s1:.2f}."
        invalidation = f"Avoid or exit if price reclaims pivot {pivot:.2f} or takes out the opening-range high after the trigger."
        management = f"Trail with 15m structure / ST(5,1.5). Partial around 1:1 or near S1 {s1:.2f}; keep runner only on a trend day."
    else:
        confidence = "low"
        setup = "No high-POP directional setup yet"
        trigger = f"Wait 30 minutes. Trade only after price clearly accepts above R1 {r1:.2f} or below S1 {s1:.2f}, or shows repeated respect around pivot {pivot:.2f}."
        invalidation = "Skip if the first hour remains trapped around pivot / CPR with no clear acceptance."
        management = "On mixed days, keep size small or skip entirely rather than forcing a directional view."
    return IndexTradePlan(
        symbol=snapshot.symbol,
        bias=snapshot.bias,
        confidence=confidence,
        setup=setup,
        trigger_after_30m=trigger,
        invalidation=invalidation,
        management=management,
        option_plan="",
    )


def choose_vix_regime(vix_value: float | None) -> str:
    if vix_value is None:
        return "normal"
    if vix_value >= 18:
        return "high"
    if vix_value <= 13:
        return "low"
    return "normal"


def resolve_prev_close(quote: dict[str, Any], daily_candles: list[Candle]) -> float:
    ohlc = quote.get("ohlc", {})
    ltp = float(quote["last_price"])
    raw_close = float(ohlc.get("close") or 0.0)
    net_change = float(quote.get("net_change") or 0.0)
    if raw_close and abs(raw_close - ltp) > 1e-9:
        return raw_close
    if net_change:
        derived = ltp - net_change
        if derived > 0:
            return derived
    if len(daily_candles) >= 2:
        return daily_candles[-2].close
    return raw_close or ltp


def build_snapshot(
    symbol: str,
    name: str,
    instrument_key: str,
    quote: dict[str, Any] | None,
    daily_candles: list[Candle],
    candles_15m: list[Candle],
    nifty_pct_change: float,
    narrow_cpr_threshold_pct: float,
    vix_regime: str,
    weekday: int,
) -> IntradaySnapshot:
    if quote is None:
        raise RuntimeError(f"Missing live quote for {symbol}")
    if len(daily_candles) < 100 or len(candles_15m) < 5:
        raise RuntimeError(f"Not enough candles for {symbol}")
    intraday_fresh = candle_date(candles_15m[-1]) == date.today()

    ohlc = quote.get("ohlc", {})
    ltp = float(quote["last_price"])
    open_price = float(ohlc["open"])
    day_high = float(ohlc["high"])
    day_low = float(ohlc["low"])
    prev_close = resolve_prev_close(quote, daily_candles)
    average_price = float(quote.get("average_price") or 0.0)
    volume = float(quote.get("volume") or 0.0)
    pct_change = ((ltp - prev_close) / prev_close) * 100 if prev_close else 0.0
    rs_vs_nifty = pct_change - nifty_pct_change

    d_closes = [c.close for c in daily_candles]
    ma20 = sma(d_closes, 20) or d_closes[-1]
    ma50 = sma(d_closes, 50) or d_closes[-1]
    ma100 = sma(d_closes, 100) or d_closes[-1]
    daily_rsi = rsi(d_closes, 14)
    daily_adx, plus_di, minus_di = adx(daily_candles, 14)

    prev_day = daily_candles[-2]
    _pivot, bc, tc, r1, s1 = compute_cpr(prev_day)
    cpr_width_pct = (abs(tc - bc) / prev_day.close) * 100 if prev_day.close else None
    cpr_tag = "narrow" if cpr_width_pct is not None and cpr_width_pct <= narrow_cpr_threshold_pct else "normal"

    intraday_st_slow = supertrend(candles_15m, 5, 3.0)
    intraday_st_fast = supertrend(candles_15m, 5, 1.5)
    st_slow = next((value for value in reversed(intraday_st_slow) if value is not None), None)
    st_fast = next((value for value in reversed(intraday_st_fast) if value is not None), None)

    recent = candles_15m[-8:]
    prior = candles_15m[:-1]
    recent_high = max(c.high for c in recent[:-1]) if len(recent) > 1 else recent[-1].high
    recent_low = min(c.low for c in recent[:-1]) if len(recent) > 1 else recent[-1].low
    micro_swing_high = max(c.high for c in prior[-4:]) if len(prior) >= 4 else recent_high
    micro_swing_low = min(c.low for c in prior[-4:]) if len(prior) >= 4 else recent_low

    ema20_15 = sma([c.close for c in candles_15m], 20) or candles_15m[-1].close
    ema50_15 = sma([c.close for c in candles_15m], 50) or candles_15m[-1].close

    score = 0
    notes: list[str] = []
    if pct_change > 0:
        score += 1
        notes.append("above previous close")
    elif pct_change < 0:
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
    if day_high and ltp >= day_high - ((day_high - day_low) * 0.20):
        score += 1
        notes.append("near day high")
    if day_low and ltp <= day_low + ((day_high - day_low) * 0.20):
        score -= 1
        notes.append("near day low")
    if rs_vs_nifty >= 0.5:
        score += 2
        notes.append("outperforming Nifty intraday")
    elif rs_vs_nifty <= -0.5:
        score -= 2
        notes.append("underperforming Nifty intraday")
    if ltp > ma20 > ma50:
        score += 1
        notes.append("daily trend aligned up")
    elif ltp < ma20 < ma50:
        score -= 1
        notes.append("daily trend aligned down")
    if ema20_15 > ema50_15:
        score += 1
        notes.append("15m trend aligned up")
    elif ema20_15 < ema50_15:
        score -= 1
        notes.append("15m trend aligned down")
    if st_slow is not None:
        if ltp > st_slow:
            score += 1
        elif ltp < st_slow:
            score -= 1
    if cpr_tag == "narrow":
        if score > 0:
            score += 1
        elif score < 0:
            score -= 1
        notes.append("narrow CPR")
    if daily_adx is not None and daily_adx >= 20:
        if plus_di is not None and minus_di is not None:
            if plus_di > minus_di:
                score += 1
            elif minus_di > plus_di:
                score -= 1

    if score >= 5:
        bias = "bullish"
    elif score <= -5:
        bias = "bearish"
    else:
        bias = "neutral"

    entry = stop = t1 = t2 = rr_one = next_key = trail = None
    stop_basis = "-"
    actionable = False
    action_reason = "Bias is not strong enough."

    if intraday_fresh and bias == "bullish":
        entry = recent_high
        stop_candidates = []
        if micro_swing_low < entry:
            stop_candidates.append((micro_swing_low, "micro 15m swing low"))
        if st_slow is not None and st_slow < entry:
            stop_candidates.append((st_slow, "ST(5,3)"))
        if stop_candidates:
            stop, stop_basis = max(stop_candidates, key=lambda item: item[0])
            risk = entry - stop
            if risk > 0:
                rr_one = entry + risk
                key_levels = sorted(level for level in {day_high, prev_day.high, r1} if level > entry)
                next_key = key_levels[0] if key_levels else rr_one
                t1 = min(rr_one, next_key) if next_key > entry else rr_one
                higher_levels = [level for level in key_levels if t1 is not None and level > t1]
                t2 = higher_levels[0] if higher_levels else entry + (2 * risk)
                trail = st_fast if st_fast is not None and st_fast < ltp else st_slow
                actionable = cpr_tag == "narrow" and rs_vs_nifty > 0 and t1 > entry
                action_reason = "Narrow CPR and positive relative strength support continuation." if actionable else "Needs narrow CPR plus positive relative strength."
    elif intraday_fresh and bias == "bearish":
        entry = recent_low
        stop_candidates = []
        if micro_swing_high > entry:
            stop_candidates.append((micro_swing_high, "micro 15m swing high"))
        if st_slow is not None and st_slow > entry:
            stop_candidates.append((st_slow, "ST(5,3)"))
        if stop_candidates:
            stop, stop_basis = min(stop_candidates, key=lambda item: item[0])
            risk = stop - entry
            if risk > 0:
                rr_one = entry - risk
                key_levels = sorted((level for level in {day_low, prev_day.low, s1} if level < entry), reverse=True)
                next_key = key_levels[0] if key_levels else rr_one
                t1 = max(rr_one, next_key) if next_key < entry else rr_one
                lower_levels = [level for level in key_levels if t1 is not None and level < t1]
                t2 = lower_levels[0] if lower_levels else entry - (2 * risk)
                trail = st_fast if st_fast is not None and st_fast > ltp else st_slow
                actionable = cpr_tag == "narrow" and rs_vs_nifty < 0 and t1 < entry
                action_reason = "Narrow CPR and negative relative strength support continuation." if actionable else "Needs narrow CPR plus negative relative strength."

    if not intraday_fresh:
        actionable = False
        action_reason = "15m intraday candles are stale from a prior session; use live chart confirmation instead."

    return IntradaySnapshot(
        symbol=symbol,
        name=name,
        instrument_key=instrument_key,
        ltp=ltp,
        open_price=open_price,
        day_high=day_high,
        day_low=day_low,
        prev_close=prev_close,
        average_price=average_price,
        volume=volume,
        pct_change=pct_change,
        rs_vs_nifty=rs_vs_nifty,
        daily_ma20=ma20,
        daily_ma50=ma50,
        daily_ma100=ma100,
        daily_rsi14=daily_rsi,
        daily_adx14=daily_adx,
        plus_di=plus_di,
        minus_di=minus_di,
        st_slow=st_slow,
        st_fast=st_fast,
        pivot=_pivot,
        r1=r1,
        s1=s1,
        cpr_width_pct=cpr_width_pct,
        cpr_tag=cpr_tag,
        score=score,
        bias=bias,
        note=", ".join(notes),
        entry_trigger=entry,
        stop_loss=stop,
        stop_basis=stop_basis,
        target_1=t1,
        target_2=t2,
        rr_one=rr_one,
        next_key_level=next_key,
        trail_level=trail,
        actionable=actionable,
        action_reason=action_reason,
        option_note=intraday_option_note(symbol, bias, cpr_tag, vix_regime, weekday),
        intraday_data_fresh=intraday_fresh,
    )


def market_regime(index_snaps: list[IntradaySnapshot]) -> str:
    bullish = sum(1 for snap in index_snaps if snap.bias == "bullish")
    bearish = sum(1 for snap in index_snaps if snap.bias == "bearish")
    if bullish >= 2:
        return "bullish"
    if bearish >= 2:
        return "bearish"
    return "mixed"


def write_report(
    report_file: Path,
    regime: str,
    vix_regime: str,
    vix_value: float | None,
    weekday_name: str,
    index_snaps: list[IntradaySnapshot],
    index_plans: list[IndexTradePlan],
    bullish: list[IntradaySnapshot],
    bearish: list[IntradaySnapshot],
) -> None:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    best_longs = [snap for snap in bullish if snap.actionable][:3] or bullish[:3]
    best_shorts = [snap for snap in bearish if snap.actionable][:3] or bearish[:3]
    lines = [
        "# Stock Intraday Analyzer",
        "",
        f"Generated: {generated}",
        f"Weekday: `{weekday_name}`",
        f"Overall intraday regime: `{regime}`",
        f"India VIX regime: `{vix_regime}`" + (f" (`{vix_value:.2f}`)" if vix_value is not None else ""),
        "",
        "## Best Actionable Shortlist",
        "",
        "### Longs",
        "",
    ]
    for snap in best_longs:
        if (not snap.intraday_data_fresh) or snap.entry_trigger is None or snap.stop_loss is None or snap.target_1 is None or snap.target_2 is None:
            continue
        lines.append(
            f"- `{snap.symbol}`: score `{snap.score}`, RS vs Nifty `{snap.rs_vs_nifty:.2f}%`, CPR `{snap.cpr_tag}`; "
            f"Entry `{snap.entry_trigger:.2f}`, SL `{snap.stop_loss:.2f}`, T1 `{snap.target_1:.2f}`, T2 `{snap.target_2:.2f}`."
        )
    lines.extend(["", "### Shorts", ""])
    for snap in best_shorts:
        if (not snap.intraday_data_fresh) or snap.entry_trigger is None or snap.stop_loss is None or snap.target_1 is None or snap.target_2 is None:
            continue
        lines.append(
            f"- `{snap.symbol}`: score `{snap.score}`, RS vs Nifty `{snap.rs_vs_nifty:.2f}%`, CPR `{snap.cpr_tag}`; "
            f"Entry `{snap.entry_trigger:.2f}`, SL `{snap.stop_loss:.2f}`, T1 `{snap.target_1:.2f}`, T2 `{snap.target_2:.2f}`."
        )
    lines.extend(
        [
            "",
            "## Index Intraday Regime",
            "",
            "| Index | Bias | Score | LTP | % Change | RS vs Nifty | CPR | Entry | SL | T1 | T2 | 0DTE Note |",
            "|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---|",
        ]
    )
    for snap in index_snaps:
        lines.append(
            f"| `{snap.symbol}` | `{snap.bias}` | `{snap.score}` | `{snap.ltp:.2f}` | `{snap.pct_change:.2f}%` | `{snap.rs_vs_nifty:.2f}%` | `{snap.cpr_tag}` ({snap.cpr_width_pct:.2f}%) | "
            f"`{snap.entry_trigger:.2f}` | `{snap.stop_loss:.2f}` | `{snap.target_1:.2f}` | `{snap.target_2:.2f}` | {snap.option_note} |"
            if snap.entry_trigger is not None and snap.stop_loss is not None and snap.target_1 is not None and snap.target_2 is not None
            else f"| `{snap.symbol}` | `{snap.bias}` | `{snap.score}` | `{snap.ltp:.2f}` | `{snap.pct_change:.2f}%` | `{snap.rs_vs_nifty:.2f}%` | `{snap.cpr_tag}` ({snap.cpr_width_pct:.2f}%) | - | - | - | - | {snap.option_note} |"
        )
    lines.extend(
        [
            "",
            "## Index Daily Intraday Plan",
            "",
            "| Index | Bias | Confidence | Setup | Trigger After 30m | Invalidation | Management | Option Plan |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for plan in index_plans:
        lines.append(
            f"| `{plan.symbol}` | `{plan.bias}` | `{plan.confidence}` | {plan.setup} | {plan.trigger_after_30m} | {plan.invalidation} | {plan.management} | {plan.option_plan} |"
        )

    def add_section(title: str, snaps: list[IntradaySnapshot]) -> None:
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| Symbol | Bias | Score | % Change | RS vs Nifty | RSI | ADX | CPR | Actionable | Entry | SL | T1 | T2 | Trail | Notes |",
                "|---|---:|---:|---:|---:|---:|---:|---|---|---:|---:|---:|---:|---:|---|",
            ]
        )
        for snap in snaps:
            entry = f"`{snap.entry_trigger:.2f}`" if snap.intraday_data_fresh and snap.entry_trigger is not None else "-"
            sl = f"`{snap.stop_loss:.2f}`" if snap.intraday_data_fresh and snap.stop_loss is not None else "-"
            t1 = f"`{snap.target_1:.2f}`" if snap.intraday_data_fresh and snap.target_1 is not None else "-"
            t2 = f"`{snap.target_2:.2f}`" if snap.intraday_data_fresh and snap.target_2 is not None else "-"
            trail = f"`{snap.trail_level:.2f}`" if snap.intraday_data_fresh and snap.trail_level is not None else "-"
            stale_note = " intraday 15m data is stale." if not snap.intraday_data_fresh else ""
            lines.append(
                f"| `{snap.symbol}` | `{snap.bias}` | `{snap.score}` | `{snap.pct_change:.2f}%` | `{snap.rs_vs_nifty:.2f}%` | "
                f"`{snap.daily_rsi14:.1f}` | `{snap.daily_adx14:.1f}` | `{snap.cpr_tag}` ({snap.cpr_width_pct:.2f}%) | "
                f"`{'yes' if snap.actionable else 'no'}` ({snap.action_reason}) | {entry} | {sl} | {t1} | {t2} | {trail} | {snap.note}; stop uses {snap.stop_basis}.{stale_note} |"
            )

    add_section("Top Bullish Intraday Candidates", bullish)
    add_section("Top Bearish Intraday Candidates", bearish)
    lines.extend(
        [
            "",
            "## How To Use",
            "",
            "- This report is meant for intraday trend selection, not overnight swing positions.",
            "- `narrow CPR` gets extra weight because it often precedes stronger day expansion.",
            "- `RS vs Nifty` is the main ranking factor for stocks. Strong outperformance is favored for longs; strong underperformance is favored for shorts.",
            "- Entries, stops, and targets are derived from `15m` structure using micro swings plus `Supertrend(5,3)` and `Supertrend(5,1.5)` for the trail.",
            "- For Tuesday Nifty 0DTE and Thursday Sensex 0DTE, use the index section first before looking at individual stocks.",
            "- On non-0DTE index days, wait for the first 30 minutes and trade only after price stabilizes relative to pivot, CPR, VWAP-like average, and key moving averages.",
        ]
    )
    report_file.write_text("\n".join(lines))


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file).expanduser()
    universe_file = Path(args.universe_file).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_file = output_dir / "intraday_report.md"

    token = load_access_token(env_file)
    weekday = datetime.now().weekday()
    symbols = load_universe(universe_file)
    instruments = fetch_nse_instruments()
    stock_map = map_stock_instruments(instruments, symbols)

    keys = [row["instrument_key"] for row in stock_map.values()] + list(INDEX_KEYS.values())
    quotes = fetch_quotes(token, keys)
    nifty_quote = find_quote(quotes, INDEX_KEYS["NIFTY"], "NIFTY")
    if nifty_quote is None:
        raise RuntimeError("Missing live Nifty quote")
    nifty_daily_candles = fetch_candles(token, INDEX_KEYS["NIFTY"], "days", "1", 180)
    nifty_prev_close = resolve_prev_close(nifty_quote, nifty_daily_candles)
    nifty_ltp = float(nifty_quote["last_price"])
    nifty_pct_change = ((nifty_ltp - nifty_prev_close) / nifty_prev_close) * 100 if nifty_prev_close else 0.0
    vix_quote = find_quote(quotes, INDEX_KEYS["INDIAVIX"], "INDIAVIX")
    vix_value = float(vix_quote["last_price"]) if vix_quote and vix_quote.get("last_price") is not None else None
    vix_regime = choose_vix_regime(vix_value)

    index_snaps: list[IntradaySnapshot] = []
    for label, key in (("NIFTY", INDEX_KEYS["NIFTY"]), ("BANKNIFTY", INDEX_KEYS["BANKNIFTY"]), ("SENSEX", INDEX_KEYS["SENSEX"])):
        quote = find_quote(quotes, key, label)
        daily_candles = nifty_daily_candles if label == "NIFTY" else fetch_candles(token, key, "days", "1", 180)
        candles_15m = fetch_candles(token, key, "minutes", "15", 12)
        index_snaps.append(
            build_snapshot(label, label, key, quote, daily_candles, candles_15m, nifty_pct_change, args.narrow_cpr_threshold_pct, vix_regime, weekday)
        )

    stock_snaps: list[IntradaySnapshot] = []
    for symbol in symbols:
        row = stock_map[symbol]
        quote = find_quote(quotes, row["instrument_key"], row["trading_symbol"])
        try:
            daily_candles = fetch_candles(token, row["instrument_key"], "days", "1", 180)
            candles_15m = fetch_candles(token, row["instrument_key"], "minutes", "15", 12)
            stock_snaps.append(
                build_snapshot(
                    symbol,
                    row.get("name") or symbol,
                    row["instrument_key"],
                    quote,
                    daily_candles,
                    candles_15m,
                    nifty_pct_change,
                    args.narrow_cpr_threshold_pct,
                    vix_regime,
                    weekday,
                )
            )
        except Exception:
            continue

    bullish = sorted(
        [snap for snap in stock_snaps if snap.bias == "bullish"],
        key=lambda item: (item.actionable, item.rs_vs_nifty, item.score),
        reverse=True,
    )[: args.top]
    bearish = sorted(
        [snap for snap in stock_snaps if snap.bias == "bearish"],
        key=lambda item: (item.actionable, -item.rs_vs_nifty, -item.score),
        reverse=True,
    )[: args.top]
    regime = market_regime(index_snaps)
    index_plans = []
    for snap in index_snaps:
        plan = build_index_trade_plan(snap, weekday)
        plan.option_plan = index_option_plan(snap.symbol, snap.bias, vix_regime, weekday)
        index_plans.append(plan)

    write_report(report_file, regime, vix_regime, vix_value, datetime.now().strftime("%A"), index_snaps, index_plans, bullish, bearish)
    print(f"Wrote report to {report_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
