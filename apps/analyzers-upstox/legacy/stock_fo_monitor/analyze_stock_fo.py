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
OPTION_CHAIN_URL = "https://api.upstox.com/v2/option/chain"
ENV_KEYS = ("UPSTOX_ACCESS_TOKEN", "ACCESS_TOKEN", "UPSTOX_TOKEN")
INDEX_KEYS = {
    "NIFTY50": "NSE_INDEX|Nifty 50",
    "NIFTY500": "NSE_INDEX|Nifty 500",
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
    oi: float = 0.0


@dataclass
class TrendSnapshot:
    symbol: str
    instrument_key: str
    name: str
    ltp: float
    close: float
    ma8: float
    ma20: float
    ma50: float
    ma100: float
    supertrend: float | None
    rsi14: float | None
    adx14: float | None
    plus_di: float | None
    minus_di: float | None
    high_52w: float
    low_52w: float
    dist_to_high_pct: float
    dist_to_low_pct: float
    rs_vs_nifty50_20: float | None
    rs_vs_nifty500_20: float | None
    rs_vs_nifty50_60: float | None
    rs_vs_nifty500_60: float | None
    score: int
    bias: str
    setup: str
    comment: str
    expiry: str = "-"
    spread: str = "-"
    max_profit: float | None = None
    max_loss: float | None = None
    pop: float | None = None
    rr_ratio: float | None = None
    liquidity_score: float | None = None
    net_delta: float | None = None
    net_theta: float | None = None
    net_vega: float | None = None
    net_gamma: float | None = None
    atm_iv: float | None = None
    skew_put_call: float | None = None
    term_structure: float | None = None
    lot_size: int | None = None
    max_profit_rupees: float | None = None
    max_loss_rupees: float | None = None
    stop_trigger: str = "-"
    adjustment_trigger: str = "-"
    oi_change_pct: float | None = None
    oi_interpretation: str = "-"
    vol_comment: str = "-"


@dataclass
class OptionSurfaceSnapshot:
    expiry: str
    atm_iv: float | None
    put_iv: float | None
    call_iv: float | None
    put_call_skew: float | None
    term_vs_next: float | None
    liquidity_score: float


@dataclass
class SpreadCandidate:
    spread_type: str
    expiry: str
    legs: list[tuple[str, float]]
    debit_or_credit: float
    max_profit: float
    max_loss: float
    pop: float
    rr_ratio: float
    liquidity_score: float
    iv_used: float | None
    net_delta: float | None = None
    net_theta: float | None = None
    net_vega: float | None = None
    net_gamma: float | None = None


@dataclass
class IndexStrategyCandidate:
    underlying: str
    strategy_name: str
    expiry: str
    structure: str
    net_credit: float | None
    max_profit: float | None
    max_loss: float | None
    pop: float | None
    net_delta: float | None
    net_theta: float | None
    net_vega: float | None
    note: str
    lot_size: int | None = None
    max_profit_rupees: float | None = None
    max_loss_rupees: float | None = None
    stop_trigger: str = "-"
    adjustment_trigger: str = "-"


@dataclass
class IndexCampaign:
    underlying: str
    leg1: IndexStrategyCandidate
    leg2: IndexStrategyCandidate
    combined_outlook: str
    combined_behavior: str
    add_leg2_when: str
    risk_note: str
    payoff_summary: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Analyze Nifty F&O stocks and major indices for weekly/monthly option setups.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--universe-file", default=str(DEFAULT_UNIVERSE_FILE))
    parser.add_argument("--output-dir", default=str(LEGACY_OUTPUT_ROOT / "stock-fo"))
    parser.add_argument("--mode", choices=("weekly", "monthly"), default="weekly")
    parser.add_argument("--top", type=int, default=8, help="Number of bullish and bearish candidates to show.")
    return parser.parse_args()


def load_access_token(env_file: Path) -> str:
    text = env_file.read_text()
    for key in ENV_KEYS:
        match = re.search(rf"^{key}=(.+)$", text, re.M)
        if match:
            return match.group(1).strip().strip('"').strip("'")
    raise RuntimeError(f"Could not find any of {ENV_KEYS} in {env_file}")


def load_universe(path: Path) -> list[str]:
    symbols = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        symbols.append(line)
    return symbols


def fetch_nse_instruments() -> list[dict[str, Any]]:
    response = requests.get(NSE_INSTRUMENT_URL, timeout=30)
    response.raise_for_status()
    return json.loads(gzip.decompress(response.content))


def map_stock_instruments(instruments: list[dict[str, Any]], symbols: list[str]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    desired = set(symbols)
    for row in instruments:
        if row.get("segment") != "NSE_EQ" or row.get("instrument_type") != "EQ":
            continue
        symbol = row.get("trading_symbol")
        if symbol in desired:
            lookup[symbol] = row
    missing = desired - set(lookup)
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


def fetch_daily_candles(token: str, instrument_key: str, days_back: int = 420) -> list[Candle]:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    end_date = date.today()
    start_date = end_date - timedelta(days=days_back)
    encoded = quote(instrument_key, safe="")
    url = f"{HISTORICAL_URL}/{encoded}/days/1/{end_date.isoformat()}/{start_date.isoformat()}"
    response = requests.get(url, headers=headers, timeout=20)
    response.raise_for_status()
    payload = response.json().get("data", {}).get("candles", [])
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
        for row in payload
    ]
    candles.sort(key=lambda item: item.ts)
    return candles


def map_futures_instruments(instruments: list[dict[str, Any]], symbols: list[str]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    desired = set(symbols)
    for symbol in desired:
        matches = [
            row
            for row in instruments
            if row.get("segment") == "NSE_FO"
            and row.get("instrument_type") == "FUT"
            and row.get("underlying_symbol") == symbol
        ]
        matches.sort(key=lambda row: epoch_ms_to_date(row.get("expiry")) or date.max)
        if matches:
            lookup[symbol] = matches[0]
    return lookup


def classify_oi(price_change_pct: float | None, oi_change_pct: float | None) -> str:
    if price_change_pct is None or oi_change_pct is None:
        return "-"
    if price_change_pct > 0 and oi_change_pct > 0:
        return "Long buildup"
    if price_change_pct < 0 and oi_change_pct > 0:
        return "Short buildup"
    if price_change_pct > 0 and oi_change_pct < 0:
        return "Short covering"
    if price_change_pct < 0 and oi_change_pct < 0:
        return "Long unwinding"
    return "Neutral"


def volatility_interpretation(vix_regime: str, atm_iv: float | None, skew: float | None, term: float | None) -> str:
    parts: list[str] = []
    if vix_regime == "high":
        parts.append("High VIX favors premium selling and wider ranges.")
    elif vix_regime == "low":
        parts.append("Low VIX favors debit spreads when the directional trend is strong.")
    else:
        parts.append("Normal VIX supports mixed spread selection.")

    if skew is not None:
        if skew > 0.01:
            parts.append("Put skew is richer than call skew, indicating downside hedging demand.")
        elif skew < -0.01:
            parts.append("Call skew is richer than put skew, indicating upside speculation or supply imbalance.")

    if term is not None:
        if term > 0.01:
            parts.append("Front expiry IV is richer than next expiry, so near-term premium is elevated.")
        elif term < -0.01:
            parts.append("Next expiry IV is richer than front expiry, so medium-term uncertainty is being priced more heavily.")

    if atm_iv is not None:
        parts.append(f"ATM IV is about {atm_iv:.1%}.")
    return " ".join(parts)


def fetch_option_chain(token: str, instrument_key: str, expiry: str) -> list[dict[str, Any]]:
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}"}
    response = requests.get(
        OPTION_CHAIN_URL,
        headers=headers,
        params={"instrument_key": instrument_key, "expiry_date": expiry},
        timeout=20,
    )
    response.raise_for_status()
    return response.json().get("data", [])


def epoch_ms_to_date(value: Any) -> date | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value) / 1000).date()
    except Exception:
        return None


def option_expiries(instruments: list[dict[str, Any]], underlying_symbol: str) -> list[date]:
    expiries = {
        epoch_ms_to_date(row.get("expiry"))
        for row in instruments
        if row.get("segment") == "NSE_FO"
        and row.get("instrument_type") in {"CE", "PE"}
        and row.get("underlying_symbol") == underlying_symbol
    }
    return sorted(exp for exp in expiries if exp is not None)


def option_lot_size(instruments: list[dict[str, Any]], underlying_symbol: str, expiry: date | None = None) -> int | None:
    rows = [
        row
        for row in instruments
        if row.get("segment") == "NSE_FO"
        and row.get("instrument_type") in {"CE", "PE"}
        and row.get("underlying_symbol") == underlying_symbol
    ]
    if expiry is not None:
        rows = [row for row in rows if epoch_ms_to_date(row.get("expiry")) == expiry]
    if not rows:
        return None
    lot = rows[0].get("lot_size")
    return int(lot) if lot is not None else None


def choose_expiry(expiries: list[date], mode: str) -> date | None:
    if not expiries:
        return None
    today = date.today()
    future = [exp for exp in expiries if exp >= today]
    if not future:
        return expiries[-1]
    if mode == "weekly":
        return future[0]
    monthly = [exp for exp in future if (exp - today).days >= 20]
    return monthly[0] if monthly else future[-1]


def choose_index_ratio_expiry(expiries: list[date]) -> date | None:
    if not expiries:
        return None
    today = date.today()
    future = [exp for exp in expiries if exp >= today]
    if not future:
        return expiries[-1]
    two_month_style = [exp for exp in future if (exp - today).days >= 45]
    return two_month_style[0] if two_month_style else future[-1]


def choose_index_condor_expiry(expiries: list[date]) -> date | None:
    if not expiries:
        return None
    today = date.today()
    future = [exp for exp in expiries if exp >= today]
    if not future:
        return expiries[-1]
    next_month_style = [exp for exp in future if (exp - today).days >= 20]
    return next_month_style[0] if next_month_style else future[-1]


def monthly_expiries(expiries: list[date]) -> list[date]:
    by_month: dict[tuple[int, int], date] = {}
    for exp in expiries:
        key = (exp.year, exp.month)
        previous = by_month.get(key)
        if previous is None or exp > previous:
            by_month[key] = exp
    return sorted(by_month.values())


def norm_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def mid_price(market_data: dict[str, Any]) -> float:
    bid = float(market_data.get("bid_price") or 0.0)
    ask = float(market_data.get("ask_price") or 0.0)
    ltp = float(market_data.get("ltp") or 0.0)
    if bid and ask:
        return (bid + ask) / 2
    return ltp or bid or ask


def leg_iv(record: dict[str, Any], opt_type: str) -> float | None:
    greeks = record.get("call_options", {}).get("option_greeks", {}) if opt_type == "CE" else record.get("put_options", {}).get("option_greeks", {})
    iv = greeks.get("iv")
    return float(iv) / 100 if iv not in (None, 0, 0.0) else None


def leg_market(record: dict[str, Any], opt_type: str) -> dict[str, Any]:
    return record.get("call_options", {}).get("market_data", {}) if opt_type == "CE" else record.get("put_options", {}).get("market_data", {})


def leg_greeks(record: dict[str, Any], opt_type: str) -> dict[str, float]:
    greeks = record.get("call_options", {}).get("option_greeks", {}) if opt_type == "CE" else record.get("put_options", {}).get("option_greeks", {})
    return {
        "delta": float(greeks.get("delta") or 0.0),
        "theta": float(greeks.get("theta") or 0.0),
        "vega": float(greeks.get("vega") or 0.0),
        "gamma": float(greeks.get("gamma") or 0.0),
    }


def closest_chain_record(chain: list[dict[str, Any]], spot: float) -> dict[str, Any]:
    return min(chain, key=lambda row: abs(float(row["strike_price"]) - spot))


def summarize_surface(chain: list[dict[str, Any]], expiry: date, next_chain: list[dict[str, Any]] | None = None) -> OptionSurfaceSnapshot:
    if not chain:
        return OptionSurfaceSnapshot(expiry=expiry.isoformat(), atm_iv=None, put_iv=None, call_iv=None, put_call_skew=None, term_vs_next=None, liquidity_score=0.0)
    spot = float(chain[0]["underlying_spot_price"])
    atm_record = closest_chain_record(chain, spot)
    atm_call_iv = leg_iv(atm_record, "CE")
    atm_put_iv = leg_iv(atm_record, "PE")
    atm_iv = None
    if atm_call_iv is not None and atm_put_iv is not None:
        atm_iv = (atm_call_iv + atm_put_iv) / 2
    elif atm_call_iv is not None:
        atm_iv = atm_call_iv
    elif atm_put_iv is not None:
        atm_iv = atm_put_iv
    skew = None
    if atm_put_iv is not None and atm_call_iv is not None:
        skew = atm_put_iv - atm_call_iv
    next_term = None
    if next_chain:
        next_atm = closest_chain_record(next_chain, float(next_chain[0]["underlying_spot_price"]))
        next_call_iv = leg_iv(next_atm, "CE")
        next_put_iv = leg_iv(next_atm, "PE")
        next_iv = None
        if next_call_iv is not None and next_put_iv is not None:
            next_iv = (next_call_iv + next_put_iv) / 2
        elif next_call_iv is not None:
            next_iv = next_call_iv
        elif next_put_iv is not None:
            next_iv = next_put_iv
        if atm_iv is not None and next_iv is not None:
            next_term = atm_iv - next_iv
    liquidity = 0.0
    for record in chain:
        liquidity += float(leg_market(record, "CE").get("oi") or 0.0)
        liquidity += float(leg_market(record, "PE").get("oi") or 0.0)
    return OptionSurfaceSnapshot(
        expiry=expiry.isoformat(),
        atm_iv=atm_iv,
        put_iv=atm_put_iv,
        call_iv=atm_call_iv,
        put_call_skew=skew,
        term_vs_next=next_term,
        liquidity_score=liquidity,
    )


def estimate_pop(spot: float, breakeven: float, iv: float | None, days_to_expiry: int, bullish: bool) -> float:
    if iv is None or iv <= 0 or days_to_expiry <= 0 or spot <= 0:
        return 0.5
    sigma = spot * iv * math.sqrt(days_to_expiry / 365)
    if sigma <= 0:
        return 0.5
    z = (breakeven - spot) / sigma
    return 1 - norm_cdf(z) if bullish else norm_cdf(z)


def choose_vix_regime(vix_value: float | None) -> str:
    if vix_value is None:
        return "normal"
    if vix_value >= 18:
        return "high"
    if vix_value <= 13:
        return "low"
    return "normal"


def evaluate_spreads(chain: list[dict[str, Any]], bias: str, expiry: date, vix_regime: str) -> list[SpreadCandidate]:
    if not chain:
        return []
    chain = sorted(chain, key=lambda row: float(row["strike_price"]))
    spot = float(chain[0]["underlying_spot_price"])
    strikes = [float(row["strike_price"]) for row in chain]
    atm_idx = min(range(len(strikes)), key=lambda idx: abs(strikes[idx] - spot))
    days_to_expiry = max((expiry - date.today()).days, 1)
    preferred_credit = vix_regime == "high"

    candidates: list[SpreadCandidate] = []
    if bias == "bullish":
        spread_types = ["bull_put_credit", "bull_call_debit"] if preferred_credit else ["bull_call_debit", "bull_put_credit"]
    elif bias == "bearish":
        spread_types = ["bear_call_credit", "bear_put_debit"] if preferred_credit else ["bear_put_debit", "bear_call_credit"]
    else:
        return []

    for spread_type in spread_types:
        for width in (1, 2, 3):
            if spread_type == "bull_call_debit":
                buy_idx = max(atm_idx - 1, 0)
                sell_idx = min(buy_idx + width, len(chain) - 1)
                if sell_idx <= buy_idx:
                    continue
                buy_rec = chain[buy_idx]
                sell_rec = chain[sell_idx]
                buy_strike = float(buy_rec["strike_price"])
                sell_strike = float(sell_rec["strike_price"])
                buy_md = leg_market(buy_rec, "CE")
                sell_md = leg_market(sell_rec, "CE")
                buy_gr = leg_greeks(buy_rec, "CE")
                sell_gr = leg_greeks(sell_rec, "CE")
                debit = mid_price(buy_md) - mid_price(sell_md)
                width_pts = sell_strike - buy_strike
                max_profit = max(width_pts - debit, 0.0)
                max_loss = max(debit, 0.0)
                breakeven = buy_strike + debit
                iv = leg_iv(buy_rec, "CE") or leg_iv(sell_rec, "CE")
                pop = estimate_pop(spot, breakeven, iv, days_to_expiry, bullish=True)
                liquidity = float(buy_md.get("oi") or 0) + float(sell_md.get("oi") or 0)
                rr = (max_profit / max_loss) if max_loss > 0 else 0.0
                candidates.append(
                    SpreadCandidate(
                        spread_type="Bull call debit spread",
                        expiry=expiry.isoformat(),
                        legs=[("BUY CE", buy_strike), ("SELL CE", sell_strike)],
                        debit_or_credit=debit,
                        max_profit=max_profit,
                        max_loss=max_loss,
                        pop=pop,
                        rr_ratio=rr,
                        liquidity_score=liquidity,
                        iv_used=iv,
                        net_delta=buy_gr["delta"] - sell_gr["delta"],
                        net_theta=buy_gr["theta"] - sell_gr["theta"],
                        net_vega=buy_gr["vega"] - sell_gr["vega"],
                        net_gamma=buy_gr["gamma"] - sell_gr["gamma"],
                    )
                )
            elif spread_type == "bull_put_credit":
                sell_idx = max(atm_idx - 1, 0)
                buy_idx = max(sell_idx - width, 0)
                if sell_idx <= buy_idx:
                    continue
                sell_rec = chain[sell_idx]
                buy_rec = chain[buy_idx]
                sell_strike = float(sell_rec["strike_price"])
                buy_strike = float(buy_rec["strike_price"])
                sell_md = leg_market(sell_rec, "PE")
                buy_md = leg_market(buy_rec, "PE")
                sell_gr = leg_greeks(sell_rec, "PE")
                buy_gr = leg_greeks(buy_rec, "PE")
                credit = mid_price(sell_md) - mid_price(buy_md)
                width_pts = sell_strike - buy_strike
                max_profit = max(credit, 0.0)
                max_loss = max(width_pts - credit, 0.0)
                breakeven = sell_strike - credit
                iv = leg_iv(sell_rec, "PE") or leg_iv(buy_rec, "PE")
                pop = estimate_pop(spot, breakeven, iv, days_to_expiry, bullish=True)
                liquidity = float(buy_md.get("oi") or 0) + float(sell_md.get("oi") or 0)
                rr = (max_profit / max_loss) if max_loss > 0 else 0.0
                candidates.append(
                    SpreadCandidate(
                        spread_type="Bull put credit spread",
                        expiry=expiry.isoformat(),
                        legs=[("SELL PE", sell_strike), ("BUY PE", buy_strike)],
                        debit_or_credit=credit,
                        max_profit=max_profit,
                        max_loss=max_loss,
                        pop=pop,
                        rr_ratio=rr,
                        liquidity_score=liquidity,
                        iv_used=iv,
                        net_delta=-(sell_gr["delta"]) + buy_gr["delta"],
                        net_theta=-(sell_gr["theta"]) + buy_gr["theta"],
                        net_vega=-(sell_gr["vega"]) + buy_gr["vega"],
                        net_gamma=-(sell_gr["gamma"]) + buy_gr["gamma"],
                    )
                )
            elif spread_type == "bear_put_debit":
                buy_idx = min(atm_idx + 1, len(chain) - 1)
                sell_idx = max(buy_idx - width, 0)
                if buy_idx <= sell_idx:
                    continue
                buy_rec = chain[buy_idx]
                sell_rec = chain[sell_idx]
                buy_strike = float(buy_rec["strike_price"])
                sell_strike = float(sell_rec["strike_price"])
                buy_md = leg_market(buy_rec, "PE")
                sell_md = leg_market(sell_rec, "PE")
                buy_gr = leg_greeks(buy_rec, "PE")
                sell_gr = leg_greeks(sell_rec, "PE")
                debit = mid_price(buy_md) - mid_price(sell_md)
                width_pts = buy_strike - sell_strike
                max_profit = max(width_pts - debit, 0.0)
                max_loss = max(debit, 0.0)
                breakeven = buy_strike - debit
                iv = leg_iv(buy_rec, "PE") or leg_iv(sell_rec, "PE")
                pop = estimate_pop(spot, breakeven, iv, days_to_expiry, bullish=False)
                liquidity = float(buy_md.get("oi") or 0) + float(sell_md.get("oi") or 0)
                rr = (max_profit / max_loss) if max_loss > 0 else 0.0
                candidates.append(
                    SpreadCandidate(
                        spread_type="Bear put debit spread",
                        expiry=expiry.isoformat(),
                        legs=[("BUY PE", buy_strike), ("SELL PE", sell_strike)],
                        debit_or_credit=debit,
                        max_profit=max_profit,
                        max_loss=max_loss,
                        pop=pop,
                        rr_ratio=rr,
                        liquidity_score=liquidity,
                        iv_used=iv,
                        net_delta=buy_gr["delta"] - sell_gr["delta"],
                        net_theta=buy_gr["theta"] - sell_gr["theta"],
                        net_vega=buy_gr["vega"] - sell_gr["vega"],
                        net_gamma=buy_gr["gamma"] - sell_gr["gamma"],
                    )
                )
            elif spread_type == "bear_call_credit":
                sell_idx = min(atm_idx + 1, len(chain) - 1)
                buy_idx = min(sell_idx + width, len(chain) - 1)
                if buy_idx <= sell_idx:
                    continue
                sell_rec = chain[sell_idx]
                buy_rec = chain[buy_idx]
                sell_strike = float(sell_rec["strike_price"])
                buy_strike = float(buy_rec["strike_price"])
                sell_md = leg_market(sell_rec, "CE")
                buy_md = leg_market(buy_rec, "CE")
                sell_gr = leg_greeks(sell_rec, "CE")
                buy_gr = leg_greeks(buy_rec, "CE")
                credit = mid_price(sell_md) - mid_price(buy_md)
                width_pts = buy_strike - sell_strike
                max_profit = max(credit, 0.0)
                max_loss = max(width_pts - credit, 0.0)
                breakeven = sell_strike + credit
                iv = leg_iv(sell_rec, "CE") or leg_iv(buy_rec, "CE")
                pop = estimate_pop(spot, breakeven, iv, days_to_expiry, bullish=False)
                liquidity = float(buy_md.get("oi") or 0) + float(sell_md.get("oi") or 0)
                rr = (max_profit / max_loss) if max_loss > 0 else 0.0
                candidates.append(
                    SpreadCandidate(
                        spread_type="Bear call credit spread",
                        expiry=expiry.isoformat(),
                        legs=[("SELL CE", sell_strike), ("BUY CE", buy_strike)],
                        debit_or_credit=credit,
                        max_profit=max_profit,
                        max_loss=max_loss,
                        pop=pop,
                        rr_ratio=rr,
                        liquidity_score=liquidity,
                        iv_used=iv,
                        net_delta=-(sell_gr["delta"]) + buy_gr["delta"],
                        net_theta=-(sell_gr["theta"]) + buy_gr["theta"],
                        net_vega=-(sell_gr["vega"]) + buy_gr["vega"],
                        net_gamma=-(sell_gr["gamma"]) + buy_gr["gamma"],
                    )
                )

    candidates = [
        candidate
        for candidate in candidates
        if candidate.max_profit > 0 and candidate.max_loss > 0 and candidate.liquidity_score > 0
    ]
    def candidate_score(item: SpreadCandidate) -> float:
        base = (item.pop * 0.40) + (min(item.rr_ratio, 3.0) * 0.30) + (math.log10(item.liquidity_score + 1) * 0.20)
        if preferred_credit and "credit" in item.spread_type.lower():
            base += 0.20
        if (not preferred_credit) and "debit" in item.spread_type.lower():
            base += 0.20
        return base

    candidates.sort(key=candidate_score, reverse=True)
    return candidates


def nearest_index(chain: list[dict[str, Any]], target: float, side: str) -> int:
    strikes = [float(row["strike_price"]) for row in chain]
    if side == "above":
        valid = [idx for idx, strike in enumerate(strikes) if strike >= target]
        return valid[0] if valid else len(strikes) - 1
    valid = [idx for idx, strike in enumerate(strikes) if strike <= target]
    return valid[-1] if valid else 0


def build_iron_condor(chain: list[dict[str, Any]], expiry: date, width_steps: int = 2) -> IndexStrategyCandidate | None:
    if not chain:
        return None
    chain = sorted(chain, key=lambda row: float(row["strike_price"]))
    spot = float(chain[0]["underlying_spot_price"])
    put_short_idx = nearest_index(chain, spot * 0.98, "below")
    call_short_idx = nearest_index(chain, spot * 1.02, "above")
    put_long_idx = max(put_short_idx - width_steps, 0)
    call_long_idx = min(call_short_idx + width_steps, len(chain) - 1)
    if put_long_idx == put_short_idx or call_long_idx == call_short_idx:
        return None

    put_short = chain[put_short_idx]
    put_long = chain[put_long_idx]
    call_short = chain[call_short_idx]
    call_long = chain[call_long_idx]

    ps_md = leg_market(put_short, "PE")
    pl_md = leg_market(put_long, "PE")
    cs_md = leg_market(call_short, "CE")
    cl_md = leg_market(call_long, "CE")
    ps_gr = leg_greeks(put_short, "PE")
    pl_gr = leg_greeks(put_long, "PE")
    cs_gr = leg_greeks(call_short, "CE")
    cl_gr = leg_greeks(call_long, "CE")

    credit = (mid_price(ps_md) - mid_price(pl_md)) + (mid_price(cs_md) - mid_price(cl_md))
    put_width = float(put_short["strike_price"]) - float(put_long["strike_price"])
    call_width = float(call_long["strike_price"]) - float(call_short["strike_price"])
    max_loss = max(max(put_width, call_width) - credit, 0.0)
    max_profit = max(credit, 0.0)
    atm_iv = summarize_surface(chain, expiry).atm_iv
    lower_be = float(put_short["strike_price"]) - credit
    upper_be = float(call_short["strike_price"]) + credit
    pop = 0.5
    if atm_iv:
        sigma = spot * atm_iv * math.sqrt(max((expiry - date.today()).days, 1) / 365)
        if sigma > 0:
            pop = norm_cdf((upper_be - spot) / sigma) - norm_cdf((lower_be - spot) / sigma)
    short_put = float(put_short["strike_price"])
    short_call = float(call_short["strike_price"])
    adjust_low = short_put + (put_width * 0.25)
    adjust_high = short_call - (call_width * 0.25)
    stop_trigger = f"Exit if spot closes below {short_put:.0f} or above {short_call:.0f}"
    adjustment_trigger = f"Adjust if spot enters {adjust_low:.0f}..{adjust_high:.0f} challenge zone"
    return IndexStrategyCandidate(
        underlying=chain[0].get("underlying_key", "INDEX"),
        strategy_name="Iron condor",
        expiry=expiry.isoformat(),
        structure=(
            f"SELL PE {float(put_short['strike_price']):.0f} / BUY PE {float(put_long['strike_price']):.0f} / "
            f"SELL CE {float(call_short['strike_price']):.0f} / BUY CE {float(call_long['strike_price']):.0f}"
        ),
        net_credit=credit,
        max_profit=max_profit,
        max_loss=max_loss,
        pop=pop,
        net_delta=-(ps_gr["delta"]) + pl_gr["delta"] - cs_gr["delta"] + cl_gr["delta"],
        net_theta=-(ps_gr["theta"]) + pl_gr["theta"] - cs_gr["theta"] + cl_gr["theta"],
        net_vega=-(ps_gr["vega"]) + pl_gr["vega"] - cs_gr["vega"] + cl_gr["vega"],
        note="Best when the market is range-bound and IV is elevated; strict stop if one short strike is challenged.",
        stop_trigger=stop_trigger,
        adjustment_trigger=adjustment_trigger,
    )


def build_ratio_spread(chain: list[dict[str, Any]], expiry: date, bias: str) -> IndexStrategyCandidate | None:
    if not chain:
        return None
    chain = sorted(chain, key=lambda row: float(row["strike_price"]))
    spot = float(chain[0]["underlying_spot_price"])
    valid_idx = [idx for idx, row in enumerate(chain) if float(row["strike_price"]) % 500 == 0]
    if not valid_idx:
        return None
    atm_idx = min(valid_idx, key=lambda idx: abs(float(chain[idx]["strike_price"]) - spot))
    if bias == "bearish":
        buy_idx = atm_idx
        sell_idx = None
        for idx in reversed(valid_idx):
            if float(chain[buy_idx]["strike_price"]) - float(chain[idx]["strike_price"]) >= 500:
                sell_idx = idx
                break
        if sell_idx is None or buy_idx == sell_idx:
            return None
        buy_rec = chain[buy_idx]
        sell_rec = chain[sell_idx]
        buy_md = leg_market(buy_rec, "PE")
        sell_md = leg_market(sell_rec, "PE")
        buy_gr = leg_greeks(buy_rec, "PE")
        sell_gr = leg_greeks(sell_rec, "PE")
        net_credit = (2 * mid_price(sell_md)) - mid_price(buy_md)
        note = "Bearish put ratio spread: small net credit, best if downside drifts; exit if downside accelerates too quickly."
        structure = f"BUY PE {float(buy_rec['strike_price']):.0f} / SELL 2x PE {float(sell_rec['strike_price']):.0f}"
        delta = buy_gr["delta"] - (2 * sell_gr["delta"])
        theta = buy_gr["theta"] - (2 * sell_gr["theta"])
        vega = buy_gr["vega"] - (2 * sell_gr["vega"])
        stop_trigger = f"Exit if spot falls below {float(sell_rec['strike_price']):.0f} quickly"
        adjustment_trigger = f"Review if spot moves below {float(buy_rec['strike_price']):.0f} and downside momentum expands"
    else:
        buy_idx = atm_idx
        sell_idx = None
        for idx in valid_idx:
            if float(chain[idx]["strike_price"]) - float(chain[buy_idx]["strike_price"]) >= 500:
                sell_idx = idx
                break
        if sell_idx is None or buy_idx == sell_idx:
            return None
        buy_rec = chain[buy_idx]
        sell_rec = chain[sell_idx]
        buy_md = leg_market(buy_rec, "CE")
        sell_md = leg_market(sell_rec, "CE")
        buy_gr = leg_greeks(buy_rec, "CE")
        sell_gr = leg_greeks(sell_rec, "CE")
        net_credit = (2 * mid_price(sell_md)) - mid_price(buy_md)
        note = "Bullish call ratio spread: small net credit, best if upside drifts; exit if upside explodes rapidly."
        structure = f"BUY CE {float(buy_rec['strike_price']):.0f} / SELL 2x CE {float(sell_rec['strike_price']):.0f}"
        delta = buy_gr["delta"] - (2 * sell_gr["delta"])
        theta = buy_gr["theta"] - (2 * sell_gr["theta"])
        vega = buy_gr["vega"] - (2 * sell_gr["vega"])
        stop_trigger = f"Exit if spot rises above {float(sell_rec['strike_price']):.0f} quickly"
        adjustment_trigger = f"Review if spot moves above {float(buy_rec['strike_price']):.0f} and upside momentum expands"
    return IndexStrategyCandidate(
        underlying=chain[0].get("underlying_key", "INDEX"),
        strategy_name="Ratio spread",
        expiry=expiry.isoformat(),
        structure=structure,
        net_credit=net_credit,
        max_profit=None,
        max_loss=None,
        pop=None,
        net_delta=delta,
        net_theta=theta,
        net_vega=vega,
        note=note,
        stop_trigger=stop_trigger,
        adjustment_trigger=adjustment_trigger,
    )


def build_index_campaign(
    label: str,
    regime: str,
    vix_regime: str,
    condor: IndexStrategyCandidate | None,
    ratio: IndexStrategyCandidate | None,
) -> IndexCampaign | None:
    if condor is None or ratio is None:
        return None
    if regime == "bullish":
        outlook = "Bullish campaign with sideways cushion."
        behavior = "Leg 1 earns if the index stays range-bound; leg 2 adds upside participation if the move develops gradually."
        add_leg2 = "Add leg 2 only after leg 1 is stable or profitable and spot holds above the condor midpoint without a fresh volatility spike."
        risk = "If the upside move turns into a fast squeeze through the ratio short strike, exit leg 2 quickly rather than averaging blindly."
        payoff = "Sideways: favorable. Slow upside drift: strongest outcome. Fast upside breakout: manage ratio leg aggressively. Sharp downside: condor cushion helps, but the campaign weakens."
    elif regime == "bearish":
        outlook = "Bearish campaign with sideways cushion."
        behavior = "Leg 1 earns in a contained range; leg 2 adds downside participation if the decline continues in a controlled way."
        add_leg2 = "Add leg 2 only after leg 1 is stable or profitable and spot remains below the condor midpoint with volatility still supportive."
        risk = "If the downside accelerates rapidly through the ratio short strike, exit leg 2 quickly instead of carrying tail risk."
        payoff = "Sideways: favorable. Slow downside drift: strongest outcome. Fast downside flush: manage ratio leg aggressively. Sharp upside reversal: condor cushion helps, but the campaign weakens."
    else:
        outlook = "Mixed-regime campaign with optional directional lean."
        behavior = "Leg 1 is the primary income engine; leg 2 is only added if price and volatility later lean clearly one way."
        add_leg2 = "Wait for the market to choose a direction first. Add leg 2 only after a clear break from the condor center with stable skew."
        risk = "In mixed markets, skip leg 2 entirely if price keeps whipsawing or if front-month IV collapses too fast."
        payoff = "Sideways: best base case. Slow directional move after confirmation: good if leg 2 is added well. Fast breakout either way: leg 1 loses tolerance quickly, so react early."
    if vix_regime == "high":
        behavior += " High VIX means the condor should do more of the heavy lifting early."
    elif vix_regime == "low":
        behavior += " Lower VIX means the ratio leg matters more if the directional thesis strengthens."
    return IndexCampaign(
        underlying=label,
        leg1=condor,
        leg2=ratio,
        combined_outlook=outlook,
        combined_behavior=behavior,
        add_leg2_when=add_leg2,
        risk_note=risk,
        payoff_summary=payoff,
    )


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


def supertrend(candles: list[Candle], period: int = 10, multiplier: float = 3.0) -> list[float | None]:
    atr_values = atr(candles, period)
    final_upper: list[float | None] = []
    final_lower: list[float | None] = []
    trends: list[bool] = []
    output: list[float | None] = []
    for idx, candle in enumerate(candles):
        current_atr = atr_values[idx]
        if current_atr is None:
            final_upper.append(None)
            final_lower.append(None)
            trends.append(True)
            output.append(None)
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
            bull = candle.close >= fl if trends[-1] else candle.close > fu
        final_upper.append(fu)
        final_lower.append(fl)
        trends.append(bull)
        output.append(fl if bull else fu)
    return output


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) <= period:
        return None
    gains = []
    losses = []
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

    if len(dx_values) < period:
        last = dx_values[-1] if dx_values else (None, None, None)
        return last[0], last[1], last[2]

    adx_value = sum(item[0] for item in dx_values[:period]) / period
    for idx in range(period, len(dx_values)):
        adx_value = ((adx_value * (period - 1)) + dx_values[idx][0]) / period
    last_plus = dx_values[-1][1]
    last_minus = dx_values[-1][2]
    return adx_value, last_plus, last_minus


def pct_return(values: list[float], period: int) -> float | None:
    if len(values) <= period:
        return None
    start = values[-(period + 1)]
    end = values[-1]
    if start == 0:
        return None
    return ((end - start) / start) * 100


def classify_setup(bias: str, score: int, adx14: float | None, dist_to_high_pct: float, dist_to_low_pct: float, mode: str) -> tuple[str, str]:
    if bias == "bullish":
        if mode == "monthly":
            if adx14 and adx14 >= 25 and dist_to_high_pct > 5:
                return "Bull call debit spread", "Convert to butterfly after 60-70% of max value if trend stalls."
            return "Bull put credit spread", "Prefer credit spread when trend is up but stock is already near resistance."
        if adx14 and adx14 >= 25:
            return "Bull call debit spread", "For weekly trades, stronger ADX favors defined-risk debit exposure."
        return "Bull put credit spread", "Use short premium when trend is positive but momentum is only moderate."

    if bias == "bearish":
        if mode == "monthly":
            if adx14 and adx14 >= 25 and dist_to_low_pct > 5:
                return "Bear put debit spread", "Convert to put butterfly after 60-70% of max value if the move matures."
            return "Bear call credit spread", "Prefer call credit spreads when price is already near support."
        if adx14 and adx14 >= 25:
            return "Bear put debit spread", "For weekly trades, strong downside trend favors a debit spread."
        return "Bear call credit spread", "Use a call credit spread when weakness is visible but not yet accelerating."

    return "No trade", "Bias is neutral or mixed."


def build_snapshot(
    symbol: str,
    instrument_key: str,
    name: str,
    quote: dict[str, Any] | None,
    candles: list[Candle],
    nifty50_returns: dict[str, float | None],
    nifty500_returns: dict[str, float | None],
    mode: str,
) -> TrendSnapshot:
    if len(candles) < 120:
        raise RuntimeError(f"Not enough daily candles for {symbol}")
    closes = [c.close for c in candles]
    ltp = float(quote["last_price"]) if quote and quote.get("last_price") is not None else closes[-1]
    ma8 = sma(closes, 8) or closes[-1]
    ma20 = sma(closes, 20) or closes[-1]
    ma50 = sma(closes, 50) or closes[-1]
    ma100 = sma(closes, 100) or closes[-1]
    st_values = supertrend(candles, 10, 3.0)
    st = next((value for value in reversed(st_values) if value is not None), None)
    rsi14 = rsi(closes, 14)
    adx14, plus_di, minus_di = adx(candles, 14)
    high_52w = max(c.high for c in candles[-252:])
    low_52w = min(c.low for c in candles[-252:])
    dist_to_high_pct = ((high_52w - ltp) / high_52w) * 100 if high_52w else 0.0
    dist_to_low_pct = ((ltp - low_52w) / low_52w) * 100 if low_52w else 0.0
    ret20 = pct_return(closes, 20)
    ret60 = pct_return(closes, 60)
    rs_n50_20 = (ret20 - nifty50_returns["20"]) if ret20 is not None and nifty50_returns["20"] is not None else None
    rs_n500_20 = (ret20 - nifty500_returns["20"]) if ret20 is not None and nifty500_returns["20"] is not None else None
    rs_n50_60 = (ret60 - nifty50_returns["60"]) if ret60 is not None and nifty50_returns["60"] is not None else None
    rs_n500_60 = (ret60 - nifty500_returns["60"]) if ret60 is not None and nifty500_returns["60"] is not None else None

    score = 0
    notes: list[str] = []
    if ltp > ma8 > ma20 > ma50 > ma100:
        score += 4
        notes.append("full MA alignment up")
        bias = "bullish"
    elif ltp < ma8 < ma20 < ma50 < ma100:
        score -= 4
        notes.append("full MA alignment down")
        bias = "bearish"
    else:
        bias = "neutral"

    if st is not None:
        if ltp > st:
            score += 1
            notes.append("above supertrend")
        else:
            score -= 1
            notes.append("below supertrend")

    if rsi14 is not None:
        if rsi14 >= 60:
            score += 1
        elif rsi14 <= 40:
            score -= 1

    if adx14 is not None and plus_di is not None and minus_di is not None:
        if adx14 >= 20 and plus_di > minus_di:
            score += 1
        elif adx14 >= 20 and minus_di > plus_di:
            score -= 1

    if dist_to_high_pct <= 5:
        score += 1
        notes.append("near 52w high")
    if dist_to_low_pct <= 5:
        score -= 1
        notes.append("near 52w low")

    if rs_n50_20 is not None:
        score += 1 if rs_n50_20 > 0 else -1
    if rs_n500_20 is not None:
        score += 1 if rs_n500_20 > 0 else -1

    if score >= 4:
        bias = "bullish"
    elif score <= -4:
        bias = "bearish"
    else:
        bias = "neutral"

    setup, comment = classify_setup(bias, score, adx14, dist_to_high_pct, dist_to_low_pct, mode)

    return TrendSnapshot(
        symbol=symbol,
        instrument_key=instrument_key,
        name=name,
        ltp=ltp,
        close=closes[-1],
        ma8=ma8,
        ma20=ma20,
        ma50=ma50,
        ma100=ma100,
        supertrend=st,
        rsi14=rsi14,
        adx14=adx14,
        plus_di=plus_di,
        minus_di=minus_di,
        high_52w=high_52w,
        low_52w=low_52w,
        dist_to_high_pct=dist_to_high_pct,
        dist_to_low_pct=dist_to_low_pct,
        rs_vs_nifty50_20=rs_n50_20,
        rs_vs_nifty500_20=rs_n500_20,
        rs_vs_nifty50_60=rs_n50_60,
        rs_vs_nifty500_60=rs_n500_60,
        score=score,
        bias=bias,
        setup=setup,
        comment=", ".join(notes) if notes else comment,
    )


def market_regime(index_snaps: list[TrendSnapshot]) -> str:
    bullish = sum(1 for snap in index_snaps if snap.bias == "bullish")
    bearish = sum(1 for snap in index_snaps if snap.bias == "bearish")
    if bullish >= 2:
        return "bullish"
    if bearish >= 2:
        return "bearish"
    return "mixed"


def format_spread(snapshot: TrendSnapshot) -> str:
    if snapshot.spread == "-":
        return "-"
    return f"{snapshot.spread} @ {snapshot.expiry}"


def format_metrics(snapshot: TrendSnapshot) -> str:
    if snapshot.max_profit is None or snapshot.max_loss is None or snapshot.pop is None:
        return "-"
    return (
        f"POP {snapshot.pop:.1%}, RR {snapshot.rr_ratio:.2f}, MaxP {snapshot.max_profit:.2f}/{snapshot.max_profit_rupees:.0f}Rs, "
        f"MaxL {snapshot.max_loss:.2f}/{snapshot.max_loss_rupees:.0f}Rs, Δ {snapshot.net_delta:.2f}, Θ {snapshot.net_theta:.2f}, "
        f"V {snapshot.net_vega:.2f}, Γ {snapshot.net_gamma:.4f}"
    )


def write_report(
    report_file: Path,
    mode: str,
    regime: str,
    vix_regime: str,
    vix_value: float | None,
    index_snaps: list[TrendSnapshot],
    bullish: list[TrendSnapshot],
    bearish: list[TrendSnapshot],
    index_strategies: list[IndexStrategyCandidate],
    index_campaigns: list[IndexCampaign],
) -> None:
    generated = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        "# Stock F&O Analyzer",
        "",
        f"Generated: {generated}",
        f"Mode: `{mode}`",
        f"Overall market regime: `{regime}`",
        f"India VIX regime: `{vix_regime}`" + (f" (`{vix_value:.2f}`)" if vix_value is not None else ""),
        "",
        "## Volatility Interpretation",
        "",
        "- `ATM IV` tells you how expensive the near-the-money options are right now.",
        "- `Skew` is shown as put IV minus call IV near ATM.",
        "- positive skew: puts are richer than calls, often reflecting downside hedging demand.",
        "- negative skew: calls are richer than puts, often reflecting upside speculation or squeeze risk.",
        "- `Term` is front-expiry ATM IV minus next-expiry ATM IV.",
        "- positive term: near-month premium is richer than next month.",
        "- negative term: farther expiry is richer than near month.",
        "",
        "## Index Trend",
        "",
        "| Index | Bias | Score | LTP | MA Stack | RSI | ADX | Dist to 52W High | Dist to 52W Low |",
        "|---|---:|---:|---:|---|---:|---:|---:|---:|",
    ]
    for snap in index_snaps:
        ma_stack = " > ".join(f"{value:.0f}" for value in (snap.ma8, snap.ma20, snap.ma50, snap.ma100))
        lines.append(
            f"| `{snap.symbol}` | `{snap.bias}` | `{snap.score}` | `{snap.ltp:.2f}` | {ma_stack} | `{snap.rsi14:.1f}` | `{snap.adx14:.1f}` | `{snap.dist_to_high_pct:.2f}%` | `{snap.dist_to_low_pct:.2f}%` |"
        )

    lines.extend(
        [
            "",
            "## Index Option Structures",
            "",
            "| Underlying | Strategy | Expiry | Structure | Net Credit | POP | Greeks | Notes |",
            "|---|---|---|---|---:|---:|---|---|",
        ]
    )
    for item in index_strategies:
        credit = f"{item.net_credit:.2f}" if item.net_credit is not None else "-"
        pop = f"{item.pop:.1%}" if item.pop is not None else "-"
        greek_parts = []
        if item.net_delta is not None:
            greek_parts.append(f"Δ {item.net_delta:.2f}")
        if item.net_theta is not None:
            greek_parts.append(f"Θ {item.net_theta:.2f}")
        if item.net_vega is not None:
            greek_parts.append(f"V {item.net_vega:.2f}")
        if item.max_profit_rupees is not None:
            greek_parts.append(f"MaxP {item.max_profit_rupees:.0f}Rs")
        if item.max_loss_rupees is not None:
            greek_parts.append(f"MaxL {item.max_loss_rupees:.0f}Rs")
        greeks = ", ".join(greek_parts) if greek_parts else "-"
        lines.append(
            f"| `{item.underlying}` | {item.strategy_name} | `{item.expiry}` | {item.structure} | `{credit}` | `{pop}` | {greeks} | {item.note}; Stop: {item.stop_trigger}; Adjust: {item.adjustment_trigger} |"
        )

    lines.extend(
        [
            "",
            "## Index Campaign Builder",
            "",
            "| Underlying | Leg 1 (next month) | Leg 2 (month after) | Combined Outlook | Payoff Map | Add Leg 2 When | Combined Behavior | Risk Note |",
            "|---|---|---|---|---|---|---|---|",
        ]
    )
    for campaign in index_campaigns:
        leg1 = f"{campaign.leg1.strategy_name} @ `{campaign.leg1.expiry}`: {campaign.leg1.structure}"
        leg2 = f"{campaign.leg2.strategy_name} @ `{campaign.leg2.expiry}`: {campaign.leg2.structure}"
        lines.append(
            f"| `{campaign.underlying}` | {leg1} | {leg2} | {campaign.combined_outlook} | {campaign.payoff_summary} | {campaign.add_leg2_when} | {campaign.combined_behavior} | {campaign.risk_note} |"
        )

    def add_section(title: str, snaps: list[TrendSnapshot]) -> None:
        lines.extend(
            [
                "",
                f"## {title}",
                "",
                "| Symbol | Bias | Score | LTP | RSI | ADX | RS vs N50 (20D/60D) | RS vs N500 (20D/60D) | 52W High Dist | 52W Low Dist | Surface | OI Signal | Suggested Setup | Exact Spread | Metrics | Risk Triggers | Notes |",
                "|---|---:|---:|---:|---:|---:|---|---|---:|---:|---|---|---|---|---|---|---|",
            ]
        )
        for snap in snaps:
            rs50 = f"{snap.rs_vs_nifty50_20:.2f}/{snap.rs_vs_nifty50_60:.2f}" if snap.rs_vs_nifty50_20 is not None and snap.rs_vs_nifty50_60 is not None else "-"
            rs500 = f"{snap.rs_vs_nifty500_20:.2f}/{snap.rs_vs_nifty500_60:.2f}" if snap.rs_vs_nifty500_20 is not None and snap.rs_vs_nifty500_60 is not None else "-"
            surface = (
                f"ATM IV {snap.atm_iv:.1%}, skew {snap.skew_put_call:.2%}, term {snap.term_structure:.2%}"
                if snap.atm_iv is not None and snap.skew_put_call is not None and snap.term_structure is not None
                else f"ATM IV {snap.atm_iv:.1%}" if snap.atm_iv is not None else "-"
            )
            oi_signal = (
                f"{snap.oi_interpretation} ({snap.oi_change_pct:.1f}%)"
                if snap.oi_change_pct is not None and snap.oi_interpretation != "-"
                else snap.oi_interpretation
            )
            lines.append(
                f"| `{snap.symbol}` | `{snap.bias}` | `{snap.score}` | `{snap.ltp:.2f}` | `{snap.rsi14:.1f}` | `{snap.adx14:.1f}` | `{rs50}` | `{rs500}` | `{snap.dist_to_high_pct:.2f}%` | `{snap.dist_to_low_pct:.2f}%` | {surface} | {oi_signal} | {snap.setup} | {format_spread(snap)} | {format_metrics(snap)} | Stop {snap.stop_trigger}; Adjust {snap.adjustment_trigger} | {snap.comment}; {snap.vol_comment} |"
            )

    add_section("Bullish Candidates", bullish)
    add_section("Bearish Candidates", bearish)

    lines.extend(
        [
            "",
            "## How To Use",
            "",
            "- Weekly mode is for choosing trades to hold into the next week.",
            "- Monthly mode is for deciding last-week-of-month ideas to carry into next month.",
            "- In high VIX environments, the analyzer biases toward credit spreads.",
            "- In low or normal VIX environments, the analyzer prefers debit spreads when trend quality is strong.",
            "- When a debit spread moves well in your favor, the suggested workflow is to convert it into a butterfly by selling an extra farther OTM option near the next resistance/support zone.",
            "- The index campaign section pairs a next-month condor with a month-after ratio spread so we can think in staged positions rather than isolated trades.",
        ]
    )
    report_file.write_text("\n".join(lines))


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file).expanduser()
    output_dir = Path(args.output_dir).expanduser()
    universe_file = Path(args.universe_file).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_file = output_dir / f"{args.mode}_report.md"

    token = load_access_token(env_file)
    symbols = load_universe(universe_file)
    nse_instruments = fetch_nse_instruments()
    stock_map = map_stock_instruments(nse_instruments, symbols)
    futures_map = map_futures_instruments(nse_instruments, symbols + ["NIFTY", "BANKNIFTY"])

    all_keys = [row["instrument_key"] for row in stock_map.values()] + list(INDEX_KEYS.values())
    quotes = fetch_quotes(token, all_keys)
    vix_quote = find_quote(quotes, INDEX_KEYS["INDIAVIX"], "INDIAVIX")
    vix_value = float(vix_quote["last_price"]) if vix_quote and vix_quote.get("last_price") is not None else None
    vix_regime = choose_vix_regime(vix_value)

    index_candles = {name: fetch_daily_candles(token, key) for name, key in INDEX_KEYS.items()}
    nifty50_closes = [c.close for c in index_candles["NIFTY50"]]
    nifty500_closes = [c.close for c in index_candles["NIFTY500"]]
    nifty50_returns = {"20": pct_return(nifty50_closes, 20), "60": pct_return(nifty50_closes, 60)}
    nifty500_returns = {"20": pct_return(nifty500_closes, 20), "60": pct_return(nifty500_closes, 60)}

    index_snaps: list[TrendSnapshot] = []
    for label, key in (("NIFTY", INDEX_KEYS["NIFTY50"]), ("BANKNIFTY", INDEX_KEYS["BANKNIFTY"]), ("SENSEX", INDEX_KEYS["SENSEX"])):
        quote = find_quote(quotes, key, label)
        candles = index_candles["NIFTY50" if label == "NIFTY" else "BANKNIFTY" if label == "BANKNIFTY" else "SENSEX"]
        index_snaps.append(build_snapshot(label, key, label, quote, candles, nifty50_returns, nifty500_returns, args.mode))

    stock_snaps: list[TrendSnapshot] = []
    for symbol in symbols:
        row = stock_map[symbol]
        quote = find_quote(quotes, row["instrument_key"], row["trading_symbol"])
        candles = fetch_daily_candles(token, row["instrument_key"])
        stock_snaps.append(
            build_snapshot(
                symbol=symbol,
                instrument_key=row["instrument_key"],
                name=row.get("name") or symbol,
                quote=quote,
                candles=candles,
                nifty50_returns=nifty50_returns,
                nifty500_returns=nifty500_returns,
                mode=args.mode,
            )
        )

    bullish = sorted([snap for snap in stock_snaps if snap.bias == "bullish"], key=lambda snap: snap.score, reverse=True)[: args.top]
    bearish = sorted([snap for snap in stock_snaps if snap.bias == "bearish"], key=lambda snap: snap.score)[: args.top]
    regime = market_regime(index_snaps)

    expiries_by_symbol = {
        symbol: option_expiries(nse_instruments, "NIFTY" if symbol == "NIFTY" else "BANKNIFTY" if symbol == "BANKNIFTY" else symbol)
        for symbol in list(symbols) + ["NIFTY", "BANKNIFTY"]
    }

    for snap in bullish + bearish:
        expiry = choose_expiry(expiries_by_symbol.get(snap.symbol, []), args.mode)
        if not expiry:
            continue
        underlying_key = INDEX_KEYS["NIFTY50"] if snap.symbol == "NIFTY" else INDEX_KEYS["BANKNIFTY"] if snap.symbol == "BANKNIFTY" else stock_map[snap.symbol]["instrument_key"]
        try:
            chain = fetch_option_chain(token, underlying_key, expiry.isoformat())
        except Exception:
            continue
        next_expiry = None
        symbol_expiries = expiries_by_symbol.get(snap.symbol, [])
        for exp in symbol_expiries:
            if exp > expiry:
                next_expiry = exp
                break
        next_chain = None
        if next_expiry:
            try:
                next_chain = fetch_option_chain(token, underlying_key, next_expiry.isoformat())
            except Exception:
                next_chain = None
        surface = summarize_surface(chain, expiry, next_chain)
        snap.atm_iv = surface.atm_iv
        snap.skew_put_call = surface.put_call_skew
        snap.term_structure = surface.term_vs_next
        snap.vol_comment = volatility_interpretation(vix_regime, surface.atm_iv, surface.put_call_skew, surface.term_vs_next)
        candidates = evaluate_spreads(chain, snap.bias, expiry, vix_regime)
        if not candidates:
            continue
        best = candidates[0]
        lot_size = option_lot_size(nse_instruments, snap.symbol, expiry)
        snap.expiry = best.expiry
        legs = " / ".join(f"{side} {strike:.0f}" for side, strike in best.legs)
        snap.spread = legs
        snap.setup = best.spread_type
        snap.max_profit = best.max_profit
        snap.max_loss = best.max_loss
        snap.lot_size = lot_size
        snap.max_profit_rupees = best.max_profit * lot_size if lot_size is not None else None
        snap.max_loss_rupees = best.max_loss * lot_size if lot_size is not None else None
        snap.pop = best.pop
        snap.rr_ratio = best.rr_ratio
        snap.liquidity_score = best.liquidity_score
        snap.net_delta = best.net_delta
        snap.net_theta = best.net_theta
        snap.net_vega = best.net_vega
        snap.net_gamma = best.net_gamma
        if "credit" in best.spread_type.lower():
            short_strike = best.legs[0][1]
            long_strike = best.legs[1][1]
            snap.stop_trigger = f"spot breaches short strike {short_strike:.0f}"
            snap.adjustment_trigger = f"review near halfway to hedge {((short_strike + long_strike) / 2):.0f}"
        else:
            bought = best.legs[0][1]
            sold = best.legs[1][1]
            if snap.bias == "bullish":
                snap.stop_trigger = f"spot back below {bought:.0f}"
                snap.adjustment_trigger = f"consider butterfly near {sold:.0f}"
            else:
                snap.stop_trigger = f"spot back above {bought:.0f}"
                snap.adjustment_trigger = f"consider butterfly near {sold:.0f}"
        if vix_regime == "high":
            snap.comment = f"{snap.comment}; high VIX favors premium selling"
        elif vix_regime == "low":
            snap.comment = f"{snap.comment}; low VIX favors debit structures"

        fut_row = futures_map.get(snap.symbol)
        if fut_row:
            fut_candles = fetch_daily_candles(token, fut_row["instrument_key"], days_back=30)
            if len(fut_candles) >= 2:
                fut_price_change = ((fut_candles[-1].close - fut_candles[-2].close) / fut_candles[-2].close) * 100 if fut_candles[-2].close else None
                prev_oi = fut_candles[-2].oi
                curr_oi = fut_candles[-1].oi
                oi_change = ((curr_oi - prev_oi) / prev_oi) * 100 if prev_oi else None
                snap.oi_change_pct = oi_change
                snap.oi_interpretation = classify_oi(fut_price_change, oi_change)

    index_strategies: list[IndexStrategyCandidate] = []
    index_campaigns: list[IndexCampaign] = []
    for label, key in (("NIFTY", INDEX_KEYS["NIFTY50"]), ("BANKNIFTY", INDEX_KEYS["BANKNIFTY"])):
        exps = expiries_by_symbol.get(label, [])
        if not exps:
            continue
        current_exp = exps[0]
        next_exp = exps[1] if len(exps) > 1 else exps[0]
        monthly_index_exps = monthly_expiries(exps)
        campaign_leg1_exp = choose_index_condor_expiry(monthly_index_exps)
        campaign_leg2_exp = choose_index_ratio_expiry(monthly_index_exps)
        if campaign_leg1_exp == campaign_leg2_exp:
            later_months = [exp for exp in monthly_index_exps if exp > campaign_leg1_exp]
            campaign_leg2_exp = later_months[0] if later_months else campaign_leg2_exp
        try:
            current_chain = fetch_option_chain(token, key, current_exp.isoformat())
            next_chain = fetch_option_chain(token, key, next_exp.isoformat())
        except Exception:
            continue
        condor = build_iron_condor(current_chain, current_exp, width_steps=2)
        if condor:
            condor.underlying = label
            condor.lot_size = option_lot_size(nse_instruments, label, current_exp)
            if condor.lot_size is not None:
                condor.max_profit_rupees = condor.max_profit * condor.lot_size if condor.max_profit is not None else None
                condor.max_loss_rupees = condor.max_loss * condor.lot_size if condor.max_loss is not None else None
            condor.note = f"{condor.note} Current month structure."
            index_strategies.append(condor)
        ratio_bias = "bearish" if regime != "bullish" else "bullish"
        ratio = build_ratio_spread(next_chain, next_exp, ratio_bias)
        if ratio:
            ratio.underlying = label
            ratio.lot_size = option_lot_size(nse_instruments, label, next_exp)
            ratio.note = f"{ratio.note} Next month structure with small credit preference."
            index_strategies.append(ratio)

        campaign_leg1_chain = None
        campaign_leg2_chain = None
        if campaign_leg1_exp:
            try:
                campaign_leg1_chain = fetch_option_chain(token, key, campaign_leg1_exp.isoformat())
            except Exception:
                campaign_leg1_chain = None
        if campaign_leg2_exp:
            try:
                campaign_leg2_chain = fetch_option_chain(token, key, campaign_leg2_exp.isoformat())
            except Exception:
                campaign_leg2_chain = None
        if campaign_leg1_chain and campaign_leg2_chain and campaign_leg1_exp and campaign_leg2_exp:
            campaign_condor = build_iron_condor(campaign_leg1_chain, campaign_leg1_exp, width_steps=2)
            ratio_bias = "bearish" if regime != "bullish" else "bullish"
            campaign_ratio = build_ratio_spread(campaign_leg2_chain, campaign_leg2_exp, ratio_bias)
            if campaign_condor:
                campaign_condor.underlying = label
                campaign_condor.note = f"{campaign_condor.note} Leg 1 for staged campaign."
            if campaign_ratio:
                campaign_ratio.underlying = label
                campaign_ratio.note = f"{campaign_ratio.note} Leg 2 for staged campaign."
            campaign = build_index_campaign(label, regime, vix_regime, campaign_condor, campaign_ratio)
            if campaign:
                index_campaigns.append(campaign)

    write_report(report_file, args.mode, regime, vix_regime, vix_value, index_snaps, bullish, bearish, index_strategies, index_campaigns)
    print(f"Wrote report to {report_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # pragma: no cover
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
