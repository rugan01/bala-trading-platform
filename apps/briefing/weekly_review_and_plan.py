#!/usr/bin/env python3
"""
Weekly Review and Plan
======================
Builds a weekend report that combines:
- higher-timeframe index and commodity context
- global backdrop
- weekly trade review from Notion
- weekly prediction review from the platform archive
- major news / macro events from the prior week
- upcoming economic calendar items and market holidays

The operational output defaults to the balas-product-os trading-system workspace
when it exists, so the weekly artifacts live alongside the daily plans and
coaching documents.
"""

from __future__ import annotations

import argparse
import contextlib
import csv
from html import unescape
import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time as timer
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import requests

TOOLS_DIR = Path(__file__).parent
sys.path.insert(0, str(TOOLS_DIR))

REPO_ROOT = Path(__file__).resolve().parents[2]
TRADING_PLATFORM_ROOT = REPO_ROOT / "packages" / "trading_platform"
TRADING_PLATFORM_SRC = TRADING_PLATFORM_ROOT / "src"
if TRADING_PLATFORM_SRC.exists():
    sys.path.insert(0, str(TRADING_PLATFORM_SRC))

from dotenv import load_dotenv

from fno_scanner import SECTOR_INDICES, calculate_ema, fetch_market_data, get_upstox_provider
from mcx_market_analysis import DEFAULT_COMMODITIES, MCXAnalyzer
from morning_brief import PLATFORM_ENV_FILE, PREMARKET_REPORTS_ROOT, run_fno_scanner, run_global_markets
from trading_platform.archive.bootstrap import DEFAULT_DB_PATH
from trading_platform.briefs.market_event_risk import get_msci_review_events


LOG_FILE = os.path.expanduser("~/Library/Logs/weekly_review_and_plan.log")
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE),
    ],
)
logger = logging.getLogger(__name__)

HTTP_HEADERS = {"User-Agent": "Mozilla/5.0"}
NSE_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://www.nseindia.com/",
}
IST = ZoneInfo("Asia/Kolkata")
US_EASTERN = ZoneInfo("America/New_York")
INDEX_YAHOO_SYMBOLS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
    "SENSEX": "^BSESN",
}
COMMODITY_PREFIXES = (
    "GOLD",
    "SILVER",
    "CRUDEOIL",
    "NATGAS",
    "NATURALGAS",
    "ZINC",
    "COPPER",
    "ALUMIN",
    "LEAD",
    "NICKEL",
)
US_EVENT_LINKS = {
    "crude": "https://www.eia.gov/petroleum/supply/weekly/index.php",
    "natgas": "https://ir.eia.gov/ngs/schedule.html?src=email",
    "jobless": "https://www.dol.gov/newsroom/releases/eta",
    "nfp": "https://www.bls.gov/ces/",
    "fomc": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm",
    "bea": "https://www.bea.gov/news/schedule",
    "rbi_schedule": "https://www.rbi.org.in/scripts/PublicationsView.aspx?id=23139",
    "nse_board_meetings": "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings",
}
DISRUPTIVE_NEWS_TERMS = {
    "war": 8,
    "attack": 8,
    "missile": 8,
    "sanction": 7,
    "tariff": 7,
    "ceasefire": 6,
    "opec": 6,
    "oil": 5,
    "natural gas": 5,
    "crude": 5,
    "fed": 5,
    "fomc": 5,
    "rbi": 5,
    "finance ministry": 5,
    "jobs": 4,
    "jobless": 4,
    "inflation": 4,
    "cpi": 4,
    "gdp": 4,
    "earnings": 4,
    "guidance": 4,
    "results": 3,
    "downgrade": 3,
    "default": 6,
}
EARNINGS_PURPOSE_TERMS = ("financial result", "financial results", "results")


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
    return PREMARKET_REPORTS_ROOT.parent.parent


TRADING_SYSTEM_ROOT = _resolve_trading_system_root()
DEFAULT_OUTPUT_DIR = TRADING_SYSTEM_ROOT / "premarket" / "reports" / "weekly"


def latest_friday_on_or_before(target: date) -> date:
    days_since_friday = (target.weekday() - 4) % 7
    return target - timedelta(days=days_since_friday)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the weekly review and week-ahead plan.")
    parser.add_argument(
        "--week-ending",
        default=latest_friday_on_or_before(date.today()).isoformat(),
        help="Friday date for the review week in YYYY-MM-DD format. Defaults to the latest Friday on or before today.",
    )
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory to store the weekly text and JSON reports.",
    )
    parser.add_argument(
        "--no-news",
        action="store_true",
        help="Skip external news and calendar fetches.",
    )
    parser.add_argument(
        "--no-reconciliation",
        action="store_true",
        help="Skip broker-vs-Notion weekly journal reconciliation.",
    )
    return parser.parse_args()


def load_env() -> None:
    load_dotenv(PLATFORM_ENV_FILE, override=True)
    home_env = Path.home() / "balas-product-os" / ".env"
    if home_env.exists():
        load_dotenv(home_env, override=True)


def normalized_text(value: Optional[str]) -> str:
    return " ".join((value or "").split())


def strip_html(value: str) -> str:
    cleaned = re.sub(r"<script.*?</script>", " ", value, flags=re.S | re.I)
    cleaned = re.sub(r"<style.*?</style>", " ", cleaned, flags=re.S | re.I)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = normalized_text(unescape(cleaned))
    # Some schedule pages split words like "N ews" across markup boundaries.
    cleaned = re.sub(r"\b([A-Z])\s+([a-z]{2,})\b", lambda m: m.group(1) + m.group(2), cleaned)
    return cleaned


def format_pct(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.2f}%"


def format_money(value: Optional[float]) -> str:
    if value is None:
        return "N/A"
    sign = "-" if value < 0 else ""
    return f"{sign}₹{abs(value):,.2f}"


def iter_dates(start_date: date, end_date: date) -> Iterable[date]:
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def parse_et_time_label(time_label: str) -> tuple[int, int]:
    clean = normalized_text(time_label).replace(".", "").upper()
    parsed = datetime.strptime(clean, "%I:%M %p")
    return parsed.hour, parsed.minute


def to_ist_datetime(local_date: date, hour: int, minute: int, tz: ZoneInfo = US_EASTERN) -> datetime:
    return datetime(local_date.year, local_date.month, local_date.day, hour, minute, tzinfo=tz).astimezone(IST)


def make_event(
    *,
    event_date: date,
    source: str,
    title: str,
    category: str,
    url: str,
    details: str = "",
    region: str = "",
    market: str = "",
    time_ist: Optional[datetime] = None,
    window_end: Optional[date] = None,
) -> dict[str, Any]:
    payload = {
        "date": event_date.isoformat(),
        "window_end": window_end.isoformat() if window_end else None,
        "source": source,
        "title": title,
        "category": category,
        "region": region,
        "market": market,
        "details": details,
        "url": url,
        "timestamp_ist": time_ist.isoformat() if time_ist else None,
        "time_ist": time_ist.strftime("%H:%M IST") if time_ist else None,
    }
    return payload


def sort_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def sort_key(item: dict[str, Any]) -> tuple[str, str, str]:
        return (
            item.get("timestamp_ist") or f"{item.get('date', '')}T99:99:99",
            item.get("source", ""),
            item.get("title", ""),
        )

    return sorted(events, key=sort_key)


def safe_pct_change(start: float, end: float) -> float:
    if not start:
        return 0.0
    return round(((end - start) / start) * 100, 2)


def mean(values: Iterable[float]) -> Optional[float]:
    values = list(values)
    if not values:
        return None
    return sum(values) / len(values)


def median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def fetch_yahoo_history(symbol: str, *, range_name: str = "1y", interval: str = "1d") -> list[dict[str, Any]]:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
    response = requests.get(url, params={"range": range_name, "interval": interval}, headers=HTTP_HEADERS, timeout=20)
    response.raise_for_status()
    result = response.json().get("chart", {}).get("result", [{}])[0]
    timestamps = result.get("timestamp", [])
    quote = result.get("indicators", {}).get("quote", [{}])[0]
    if not timestamps or not quote:
        return []

    opens = quote.get("open", [])
    highs = quote.get("high", [])
    lows = quote.get("low", [])
    closes = quote.get("close", [])
    volumes = quote.get("volume", [])

    bars = []
    for idx, ts in enumerate(timestamps):
        close = closes[idx] if idx < len(closes) else None
        if close is None:
            continue
        dt = datetime.fromtimestamp(ts)
        bars.append(
            {
                "date": dt.date(),
                "open": float(opens[idx]) if idx < len(opens) and opens[idx] is not None else float(close),
                "high": float(highs[idx]) if idx < len(highs) and highs[idx] is not None else float(close),
                "low": float(lows[idx]) if idx < len(lows) and lows[idx] is not None else float(close),
                "close": float(close),
                "volume": int(volumes[idx] or 0) if idx < len(volumes) else 0,
            }
        )
    return bars


def build_weekly_bars(daily_bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    weekly: list[dict[str, Any]] = []
    current_key = None
    current_bar: dict[str, Any] | None = None

    for bar in daily_bars:
        week_key = bar["date"].isocalendar()[:2]
        if week_key != current_key:
            if current_bar:
                weekly.append(current_bar)
            current_key = week_key
            current_bar = {
                "date": bar["date"],
                "open": bar["open"],
                "high": bar["high"],
                "low": bar["low"],
                "close": bar["close"],
                "volume": bar["volume"],
            }
        else:
            assert current_bar is not None
            current_bar["high"] = max(current_bar["high"], bar["high"])
            current_bar["low"] = min(current_bar["low"], bar["low"])
            current_bar["close"] = bar["close"]
            current_bar["volume"] += bar["volume"]

    if current_bar:
        weekly.append(current_bar)

    return weekly


def classify_htf_bias(
    close: float,
    ema_fast: float,
    ema_mid: float,
    ema_long: float,
    weekly_close: float,
    weekly_ema_fast: float,
    weekly_ema_mid: float,
) -> str:
    if close > ema_fast > ema_mid and weekly_close > weekly_ema_fast > weekly_ema_mid:
        return "bullish"
    if close < ema_fast < ema_mid and weekly_close < weekly_ema_fast < weekly_ema_mid:
        return "bearish"
    if close > ema_fast and weekly_close > weekly_ema_fast:
        return "bullish_but_stretched"
    if close < ema_fast and weekly_close < weekly_ema_fast:
        return "bearish_but_stretched"
    return "mixed"


def analyze_index_higher_timeframe(name: str, yahoo_symbol: str) -> dict[str, Any]:
    daily_bars = fetch_yahoo_history(yahoo_symbol, range_name="1y", interval="1d")
    if len(daily_bars) < 30:
        raise ValueError(f"Not enough Yahoo history for {name}")

    closes = [bar["close"] for bar in daily_bars]
    weekly_bars = build_weekly_bars(daily_bars)
    weekly_closes = [bar["close"] for bar in weekly_bars]

    current = closes[-1]
    daily_ema_20 = calculate_ema(closes, 20)
    daily_ema_50 = calculate_ema(closes, 50)
    daily_ema_200 = calculate_ema(closes, 200)
    weekly_ema_10 = calculate_ema(weekly_closes, 10)
    weekly_ema_20 = calculate_ema(weekly_closes, 20)

    bias = classify_htf_bias(
        current,
        daily_ema_20,
        daily_ema_50,
        daily_ema_200,
        weekly_closes[-1],
        weekly_ema_10,
        weekly_ema_20,
    )

    return {
        "symbol": name,
        "current": round(current, 2),
        "daily_ema_20": round(daily_ema_20, 2),
        "daily_ema_50": round(daily_ema_50, 2),
        "daily_ema_200": round(daily_ema_200, 2),
        "weekly_ema_10": round(weekly_ema_10, 2),
        "weekly_ema_20": round(weekly_ema_20, 2),
        "week_return_pct": safe_pct_change(closes[-6], current) if len(closes) >= 6 else 0.0,
        "month_return_pct": safe_pct_change(closes[-21], current) if len(closes) >= 21 else 0.0,
        "quarter_return_pct": safe_pct_change(closes[-63], current) if len(closes) >= 63 else 0.0,
        "support_20d": round(min(closes[-20:]), 2),
        "resistance_20d": round(max(closes[-20:]), 2),
        "support_12w": round(min(weekly_closes[-12:]), 2),
        "resistance_12w": round(max(weekly_closes[-12:]), 2),
        "bias": bias,
    }


def analyze_weekly_indices() -> dict[str, Any]:
    summaries: dict[str, Any] = {}
    for name, yahoo_symbol in INDEX_YAHOO_SYMBOLS.items():
        try:
            summaries[name] = analyze_index_higher_timeframe(name, yahoo_symbol)
        except Exception as exc:
            logger.warning("Weekly index analysis failed for %s: %s", name, exc)
    return summaries


def analyze_weekly_commodities() -> dict[str, Any]:
    analyzer = MCXAnalyzer(env_file=str(PLATFORM_ENV_FILE))
    with contextlib.redirect_stdout(io.StringIO()):
        analyzer.download_instruments_master()
        quotes = analyzer.get_live_quotes()
        historical = analyzer.get_historical_data(days=140)
    summaries: dict[str, Any] = {}

    for commodity in DEFAULT_COMMODITIES:
        candles = historical.get(commodity, [])
        if len(candles) < 30:
            continue

        live_price = quotes[commodity].ltp if commodity in quotes else None
        analysis = analyzer.generate_comprehensive_analysis(commodity, candles, live_price)
        if not analysis:
            continue

        chrono_closes = [c.close for c in reversed(candles)]
        daily_bars = [
            {
                "date": candle.timestamp.date(),
                "open": candle.open,
                "high": candle.high,
                "low": candle.low,
                "close": candle.close,
                "volume": candle.volume,
            }
            for candle in reversed(candles)
        ]
        weekly_bars = build_weekly_bars(daily_bars)
        weekly_closes = [bar["close"] for bar in weekly_bars] if weekly_bars else []

        weekly_bias = "mixed"
        weekly_ema_10 = calculate_ema(weekly_closes, 10) if weekly_closes else 0.0
        weekly_ema_20 = calculate_ema(weekly_closes, 20) if weekly_closes else 0.0
        if weekly_closes:
            weekly_bias = "bullish" if weekly_closes[-1] > weekly_ema_10 else "bearish"
            if weekly_closes[-1] > weekly_ema_10 > weekly_ema_20:
                weekly_bias = "bullish"
            elif weekly_closes[-1] < weekly_ema_10 < weekly_ema_20:
                weekly_bias = "bearish"
            else:
                weekly_bias = "mixed"

        summaries[commodity] = {
            "commodity": commodity,
            "ltp": analysis.ltp,
            "bias": analysis.bias.lower(),
            "trend": analysis.trend.lower(),
            "structure": analysis.structure.lower(),
            "probability": analysis.trend_probability,
            "week_return_pct": safe_pct_change(chrono_closes[-6], chrono_closes[-1]) if len(chrono_closes) >= 6 else 0.0,
            "month_return_pct": safe_pct_change(chrono_closes[-21], chrono_closes[-1]) if len(chrono_closes) >= 21 else 0.0,
            "quarter_return_pct": safe_pct_change(chrono_closes[-63], chrono_closes[-1]) if len(chrono_closes) >= 63 else 0.0,
            "daily_support": analysis.levels.swing_low,
            "daily_resistance": analysis.levels.swing_high,
            "weekly_ema_10": round(weekly_ema_10, 2) if weekly_closes else None,
            "weekly_ema_20": round(weekly_ema_20, 2) if weekly_closes else None,
            "weekly_bias": weekly_bias,
            "trade_type": analysis.trade_type,
            "bias_reason": analysis.bias_reason,
        }

    return summaries


def _plain_text(prop: dict[str, Any], prop_type: str) -> str:
    items = prop.get(prop_type, [])
    return "".join(item.get("plain_text", "") for item in items).strip()


def notion_select(prop: dict[str, Any]) -> Optional[str]:
    value = prop.get("select")
    return value.get("name") if value else None


def notion_number(prop: dict[str, Any]) -> Optional[float]:
    return prop.get("number")


def notion_checkbox(prop: dict[str, Any]) -> Optional[bool]:
    return prop.get("checkbox")


def notion_date(prop: dict[str, Any]) -> Optional[str]:
    value = prop.get("date")
    return value.get("start") if value else None


def parse_trade_root(symbol: str) -> str:
    symbol_upper = normalized_text(symbol).upper()
    for prefix in sorted(
        (
            "BANKNIFTY",
            "FINNIFTY",
            "SENSEX",
            "NIFTY",
            "SUNPHARMA",
            "TATASTEEL",
            "RELIANCE",
            "COALINDIA",
            "MANAPPURAM",
            "EMAMILTD",
            *COMMODITY_PREFIXES,
        ),
        key=len,
        reverse=True,
    ):
        if symbol_upper.startswith(prefix):
            return prefix
    match = re.match(r"[A-Z\-]+", symbol_upper)
    return match.group(0) if match else symbol_upper


def infer_segment(symbol: str, instrument_type: Optional[str]) -> str:
    symbol_upper = normalized_text(symbol).upper()
    instrument_upper = normalized_text(instrument_type).upper()
    if symbol_upper.startswith(COMMODITY_PREFIXES) or "COMMOD" in instrument_upper or "MCX" in instrument_upper:
        return "commodity"
    if instrument_upper in {"STOCK", "EQUITY"}:
        return "equity"
    return "fno"


def parse_notion_trade_row(page: dict[str, Any]) -> dict[str, Any]:
    props = page.get("properties", {})
    label = _plain_text(props.get("Trade Label", {}), "title")
    symbol = _plain_text(props.get("Symbol", {}), "rich_text")
    account_match = re.match(r"^\[([A-Z]+)\]", label or "")
    # Current journal convention prefixes NIMMY rows explicitly. Bala rows are
    # often unlabeled in the Trade Label and should default to BALA.
    account = account_match.group(1) if account_match else "BALA"
    entry_date = notion_date(props.get("Entry Date", {}))
    exit_date = notion_date(props.get("Exit Date", {}))
    pnl = notion_number(props.get("P&L", {})) or 0.0
    fees = notion_number(props.get("Fees", {})) or 0.0
    net_pnl = pnl - fees
    pre_notes = _plain_text(props.get("Pre-trade Notes", {}), "rich_text")
    post_review = _plain_text(props.get("Post-trade Review", {}), "rich_text")
    instrument_type = notion_select(props.get("Instrument Type", {}))
    segment = infer_segment(symbol, instrument_type)

    return {
        "page_id": page.get("id"),
        "label": label,
        "account": account,
        "symbol": symbol,
        "instrument_root": parse_trade_root(symbol),
        "segment": segment,
        "direction": notion_select(props.get("Direction", {})),
        "strategy": notion_select(props.get("Strategy", {})),
        "instrument_type": instrument_type,
        "timeframe": notion_select(props.get("Timeframe", {})),
        "status": notion_select(props.get("Status", {})) or "Unknown",
        "outcome": notion_select(props.get("Outcome", {})),
        "entry_date": entry_date,
        "exit_date": exit_date,
        "pnl": pnl,
        "fees": fees,
        "net_pnl": net_pnl,
        "followed_plan": notion_checkbox(props.get("Followed Plan", {})),
        "pre_notes": pre_notes,
        "post_review": post_review,
        "notes_blob": normalized_text(f"{pre_notes} {post_review}").lower(),
    }


def fetch_weekly_notion_trades(start_date: date, end_date: date) -> list[dict[str, Any]]:
    notion_key = os.getenv("NOTION_API_KEY")
    notion_db = os.getenv("NOTION_TRADING_JOURNAL_DB")
    if not notion_key or not notion_db:
        logger.warning("Notion credentials missing; weekly trade review will be unavailable.")
        return []

    headers = {
        "Authorization": f"Bearer {notion_key}",
        "Notion-Version": "2022-06-28",
        "Content-Type": "application/json",
    }
    url = f"https://api.notion.com/v1/databases/{notion_db}/query"

    payload: dict[str, Any] = {
        "filter": {
            "and": [
                {"property": "Entry Date", "date": {"on_or_after": start_date.isoformat()}},
                {"property": "Entry Date", "date": {"on_or_before": end_date.isoformat()}},
            ]
        },
        "sorts": [{"property": "Entry Date", "direction": "ascending"}],
        "page_size": 100,
    }

    results: list[dict[str, Any]] = []
    while True:
        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
        results.extend(data.get("results", []))
        if not data.get("has_more"):
            break
        payload["start_cursor"] = data.get("next_cursor")

    return [parse_notion_trade_row(page) for page in results]


def summarize_weekly_trades(rows: list[dict[str, Any]], start_date: date, end_date: date) -> dict[str, Any]:
    if not rows:
        return {
            "week_start": start_date.isoformat(),
            "week_end": end_date.isoformat(),
            "total_rows": 0,
            "closed_rows": 0,
            "open_rows": 0,
            "net_realized_pnl": 0.0,
            "total_fees": 0.0,
            "by_account": [],
            "by_segment": [],
            "by_instrument": [],
            "top_winners": [],
            "top_losers": [],
            "rule_signals": {},
        }

    closed_rows = [row for row in rows if row["status"].lower() == "closed"]
    open_rows = [row for row in rows if row["status"].lower() != "closed"]

    def aggregate(group_key: str, dataset: list[dict[str, Any]]) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in dataset:
            buckets[item[group_key]].append(item)
        summary = []
        for key, bucket in buckets.items():
            net_values = [row["net_pnl"] for row in bucket]
            summary.append(
                {
                    group_key: key,
                    "rows": len(bucket),
                    "net_pnl": round(sum(net_values), 2),
                    "mean_pnl": round(mean(net_values) or 0.0, 2),
                    "median_pnl": round(median(net_values) or 0.0, 2),
                    "win_rate": round(sum(1 for value in net_values if value > 0) / len(net_values) * 100, 2),
                }
            )
        summary.sort(key=lambda item: item["net_pnl"], reverse=True)
        return summary

    notes_blobs = [row["notes_blob"] for row in rows]
    followed_plan_has_true = any(row["followed_plan"] is True for row in rows)
    rule_signals = {
        "averaging_mentions": sum(1 for blob in notes_blobs if "averag" in blob),
        "impulsive_mentions": sum(1 for blob in notes_blobs if any(word in blob for word in ("impuls", "revenge", "fomo", "greed", "mess"))),
        "plan_misses": sum(1 for row in rows if row["followed_plan"] is False) if followed_plan_has_true else None,
        "trade_days_over_4_rows": 0,
    }

    counts_by_day: dict[str, int] = defaultdict(int)
    for row in rows:
        if row["entry_date"]:
            counts_by_day[row["entry_date"]] += 1
    rule_signals["trade_days_over_4_rows"] = sum(1 for count in counts_by_day.values() if count > 4)

    closed_sorted = sorted(closed_rows, key=lambda item: item["net_pnl"], reverse=True)
    top_winners = [
        {
            "label": row["label"],
            "symbol": row["symbol"],
            "account": row["account"],
            "segment": row["segment"],
            "net_pnl": round(row["net_pnl"], 2),
        }
        for row in closed_sorted[:5]
    ]
    top_losers = [
        {
            "label": row["label"],
            "symbol": row["symbol"],
            "account": row["account"],
            "segment": row["segment"],
            "net_pnl": round(row["net_pnl"], 2),
        }
        for row in sorted(closed_rows, key=lambda item: item["net_pnl"])[:5]
    ]

    return {
        "week_start": start_date.isoformat(),
        "week_end": end_date.isoformat(),
        "total_rows": len(rows),
        "closed_rows": len(closed_rows),
        "open_rows": len(open_rows),
        "net_realized_pnl": round(sum(row["net_pnl"] for row in closed_rows), 2),
        "total_fees": round(sum(row["fees"] for row in rows), 2),
        "by_account": aggregate("account", closed_rows),
        "by_segment": aggregate("segment", closed_rows),
        "by_instrument": aggregate("instrument_root", closed_rows)[:10],
        "top_winners": top_winners,
        "top_losers": top_losers,
        "open_positions": [
            {
                "label": row["label"],
                "symbol": row["symbol"],
                "account": row["account"],
                "segment": row["segment"],
                "status": row["status"],
            }
            for row in open_rows[:10]
        ],
        "rule_signals": rule_signals,
    }


def run_weekly_journal_reconciliation(start_date: date, end_date: date) -> dict[str, Any]:
    script = REPO_ROOT / "apps" / "journaling" / "weekly_journal_reconciliation.py"
    output_dir = TRADING_SYSTEM_ROOT / "reconciliation" / "weekly"
    if not script.exists():
        return {
            "status": "unavailable",
            "error": f"Missing reconciliation script: {script}",
        }

    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--start-date",
            start_date.isoformat(),
            "--end-date",
            end_date.isoformat(),
            "--output",
            str(output_dir),
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=900,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        logger.warning("Weekly journal reconciliation failed: %s", detail[:1000])
        return {
            "status": "error",
            "error": detail,
        }

    latest_json = output_dir / "weekly_journal_reconciliation_latest.json"
    latest_text = output_dir / "weekly_journal_reconciliation_latest.txt"
    if not latest_json.exists():
        return {
            "status": "error",
            "error": f"Reconciliation completed but latest JSON was not found: {latest_json}",
        }

    payload = json.loads(latest_json.read_text(encoding="utf-8"))
    payload["latest_json_path"] = str(latest_json)
    payload["latest_text_path"] = str(latest_text)
    return payload


def fetch_weekly_prediction_rows(start_date: date, end_date: date) -> list[dict[str, Any]]:
    conn = sqlite3.connect(DEFAULT_DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        run_rows = conn.execute(
            """
            SELECT brief_run_id, run_date, run_timestamp
            FROM brief_runs
            WHERE session_label = 'morning_brief'
              AND run_date BETWEEN ? AND ?
            ORDER BY run_date ASC, run_timestamp DESC
            """,
            (start_date.isoformat(), end_date.isoformat()),
        ).fetchall()
        latest_runs: dict[str, str] = {}
        for row in run_rows:
            latest_runs.setdefault(row["run_date"], row["brief_run_id"])
        run_ids = list(latest_runs.values())
        if not run_ids:
            return []

        placeholders = ",".join("?" for _ in run_ids)
        query = f"""
            SELECT
                br.run_date,
                bp.prediction_id,
                bp.asset_class,
                bp.universe,
                bp.symbol,
                bp.signal_family,
                bp.predicted_direction,
                bp.regime_label,
                bp.confidence_score,
                bo.realized_direction,
                bo.bullish_correct,
                bo.bearish_correct,
                bo.score
            FROM brief_predictions bp
            JOIN brief_runs br ON br.brief_run_id = bp.brief_run_id
            LEFT JOIN brief_outcomes bo ON bo.prediction_id = bp.prediction_id
            WHERE bp.brief_run_id IN ({placeholders})
            ORDER BY br.run_date ASC, bp.asset_class, bp.signal_family, bp.symbol
        """
        rows = conn.execute(query, run_ids).fetchall()
        records = []
        for row in rows:
            predicted_direction = (row["predicted_direction"] or "").lower()
            correct = None
            if predicted_direction == "bullish" and row["bullish_correct"] is not None:
                correct = bool(row["bullish_correct"])
            elif predicted_direction == "bearish" and row["bearish_correct"] is not None:
                correct = bool(row["bearish_correct"])

            run_dt = date.fromisoformat(row["run_date"])
            records.append(
                {
                    "run_date": row["run_date"],
                    "weekday": run_dt.strftime("%A"),
                    "asset_class": row["asset_class"],
                    "universe": row["universe"],
                    "symbol": row["symbol"],
                    "signal_family": row["signal_family"],
                    "predicted_direction": predicted_direction,
                    "regime_label": row["regime_label"],
                    "confidence_score": row["confidence_score"],
                    "realized_direction": row["realized_direction"],
                    "correct": correct,
                    "score": row["score"],
                }
            )
        return records
    finally:
        conn.close()


def summarize_prediction_review(rows: list[dict[str, Any]]) -> dict[str, Any]:
    evaluated = [row for row in rows if row["correct"] is not None]
    if not rows:
        return {
            "total_predictions": 0,
            "evaluated_predictions": 0,
            "overall_hit_rate": None,
            "by_asset_class": [],
            "by_signal_family": [],
            "by_weekday": [],
            "strengths": [],
            "gaps": [],
        }

    def aggregate(group_key: str) -> list[dict[str, Any]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in evaluated:
            buckets[item[group_key] or "UNKNOWN"].append(item)
        summary = []
        for key, bucket in buckets.items():
            hit_rate = sum(1 for row in bucket if row["correct"]) / len(bucket) * 100 if bucket else 0.0
            mean_score = mean([row["score"] for row in bucket if row["score"] is not None]) or 0.0
            summary.append(
                {
                    group_key: key,
                    "evaluated": len(bucket),
                    "hit_rate": round(hit_rate, 2),
                    "mean_score": round(mean_score, 2),
                }
            )
        summary.sort(key=lambda item: (item["hit_rate"], item["evaluated"]), reverse=True)
        return summary

    by_signal = aggregate("signal_family")
    strengths = [item for item in by_signal if item["evaluated"] >= 3][:5]
    gaps = sorted([item for item in by_signal if item["evaluated"] >= 3], key=lambda item: item["hit_rate"])[:5]

    return {
        "total_predictions": len(rows),
        "evaluated_predictions": len(evaluated),
        "overall_hit_rate": round(sum(1 for row in evaluated if row["correct"]) / len(evaluated) * 100, 2) if evaluated else None,
        "by_asset_class": aggregate("asset_class"),
        "by_signal_family": by_signal,
        "by_weekday": aggregate("weekday"),
        "strengths": strengths,
        "gaps": gaps,
    }


def score_disruptive_news(item: dict[str, Any]) -> int:
    haystack = normalized_text(f"{item.get('title', '')} {item.get('source', '')}").lower()
    score = 0
    for phrase, weight in DISRUPTIVE_NEWS_TERMS.items():
        if phrase in haystack:
            score += weight
    if item.get("category") in {"geopolitics", "india_policy", "global_macro"}:
        score += 2
    return score


def parse_google_news_rss(query: str, *, limit: int = 10) -> list[dict[str, Any]]:
    url = "https://news.google.com/rss/search"
    params = {"q": query, "hl": "en-IN", "gl": "IN", "ceid": "IN:en"}
    response = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=20)
    response.raise_for_status()
    root = ET.fromstring(response.text)
    items = []
    for item in root.findall(".//item")[:limit]:
        title = normalized_text(item.findtext("title"))
        link = item.findtext("link")
        pub_date = item.findtext("pubDate")
        try:
            published_at = parsedate_to_datetime(pub_date) if pub_date else None
        except Exception:
            published_at = None
        source = title.rsplit(" - ", 1)[-1] if " - " in title else ""
        items.append(
            {
                "title": title,
                "link": link,
                "published_at": published_at.isoformat() if published_at else None,
                "source": source,
            }
        )
    return items


def fetch_top_weekly_news(limit: int = 5) -> list[dict[str, Any]]:
    query_plan = [
        ("geopolitics", "war sanctions tariffs oil shipping OPEC when:7d"),
        ("india_policy", "RBI finance ministry SEBI India market when:7d"),
        ("global_macro", "Fed jobs inflation yields GDP when:7d US global markets"),
        ("commodities", "crude oil natural gas gold silver zinc when:7d"),
        ("earnings", "India earnings guidance results when:7d market"),
    ]
    collected: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    for category, query in query_plan:
        for item in parse_google_news_rss(query, limit=5):
            title_key = re.sub(r"[^A-Z0-9]+", "", item["title"].upper())
            if not title_key or title_key in seen_titles:
                continue
            seen_titles.add(title_key)
            collected.append({**item, "category": category})

    collected.sort(key=lambda item: ((score_disruptive_news(item), item.get("published_at") or "")), reverse=True)

    filtered: list[dict[str, Any]] = []
    for item in collected:
        title_lower = item["title"].lower()
        source_lower = (item.get("source") or "").lower()
        if any(
            phrase in title_lower or phrase in source_lower
            for phrase in (
                "prediction today",
                "news and updates",
                "the new york stock exchange | nyse",
                "equitymaster",
                "tokenized commodities market",
                "global banking annual review",
                "mckinsey",
                "pluang",
                "nyse",
                "market newsletter",
                "weekly recap",
            )
        ):
            continue
        filtered.append(item)

    selected: list[dict[str, Any]] = []
    used_categories: set[str] = set()
    selected_titles: set[str] = set()
    for item in filtered:
        if item["category"] not in used_categories:
            enriched = {**item, "priority_score": score_disruptive_news(item)}
            selected.append(enriched)
            used_categories.add(item["category"])
            selected_titles.add(item["title"])
        if len(selected) >= limit:
            return selected
    for item in filtered:
        if item["title"] not in selected_titles:
            selected.append({**item, "priority_score": score_disruptive_news(item)})
            selected_titles.add(item["title"])
        if len(selected) >= limit:
            break
    return selected


def create_nse_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(NSE_HEADERS)
    session.get("https://www.nseindia.com", headers={"User-Agent": NSE_HEADERS["User-Agent"]}, timeout=20)
    return session


def fetch_eia_crude_holiday_overrides() -> list[dict[str, Any]]:
    response = requests.get("https://www.eia.gov/petroleum/supply/weekly/schedule.php", headers=HTTP_HEADERS, timeout=20)
    response.raise_for_status()
    overrides = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", response.text, flags=re.S | re.I):
        raw_text = unescape(re.sub(r"<[^>]+>", " | ", row_html))
        cells = [normalized_text(cell) for cell in raw_text.split("|") if normalized_text(cell)]
        if len(cells) < 5 or cells[0].lower().startswith("data for the week ending"):
            continue
        week_ending = datetime.strptime(cells[0], "%B %d, %Y").date()
        alternate = datetime.strptime(cells[1], "%B %d, %Y").date()
        overrides.append(
            {
                "week_ending": week_ending,
                "standard_release": week_ending + timedelta(days=5),
                "release_date": alternate,
                "release_time": cells[3],
                "release_day": cells[2],
                "holiday": cells[4],
            }
        )
    return overrides


def fetch_eia_natgas_holiday_overrides() -> list[dict[str, Any]]:
    response = requests.get("https://ir.eia.gov/ngs/schedule.html?src=email", headers=HTTP_HEADERS, timeout=20)
    response.raise_for_status()
    overrides = []
    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", response.text, flags=re.S | re.I):
        raw_text = unescape(re.sub(r"<[^>]+>", " | ", row_html)).replace("(Updated)", "")
        cells = [normalized_text(cell) for cell in raw_text.split("|") if normalized_text(cell)]
        if len(cells) < 4 or cells[0].lower().startswith("alternate release date"):
            continue
        alternate_label = cells[0].rstrip(" -")
        alternate = datetime.strptime(alternate_label, "%B %d, %Y").date()
        overrides.append(
            {
                "release_date": alternate,
                "release_time": cells[2],
                "release_day": cells[1],
                "holiday": cells[3],
            }
        )
    return overrides


def build_eia_crude_events(start_date: date, end_date: date) -> list[dict[str, Any]]:
    overrides = fetch_eia_crude_holiday_overrides()
    override_by_date = {item["release_date"]: item for item in overrides}
    suppressed_standard = {item["standard_release"] for item in overrides}
    events = []

    for current in iter_dates(start_date, end_date):
        if current in override_by_date:
            override = override_by_date[current]
            hour, minute = parse_et_time_label(override["release_time"])
            details = (
                f"Holiday-week crude inventory release ({override['holiday']}); "
                f"summary release at {override['release_time']} ET."
            )
            events.append(
                make_event(
                    event_date=current,
                    source="EIA",
                    title="Weekly Petroleum Status Report",
                    category="commodity",
                    region="US",
                    market="crude oil",
                    details=details,
                    url=US_EVENT_LINKS["crude"],
                    time_ist=to_ist_datetime(current, hour, minute),
                )
            )
            continue
        if current.weekday() == 2 and current not in suppressed_standard:
            events.append(
                make_event(
                    event_date=current,
                    source="EIA",
                    title="Weekly Petroleum Status Report",
                    category="commodity",
                    region="US",
                    market="crude oil",
                    details="Standard weekly crude inventory release at 10:30 AM ET.",
                    url=US_EVENT_LINKS["crude"],
                    time_ist=to_ist_datetime(current, 10, 30),
                )
            )
    return events


def build_eia_natgas_events(start_date: date, end_date: date) -> list[dict[str, Any]]:
    overrides = fetch_eia_natgas_holiday_overrides()
    override_by_date = {item["release_date"]: item for item in overrides}
    suppressed_weeks = {item["release_date"].isocalendar()[:2] for item in overrides}
    events = []

    for current in iter_dates(start_date, end_date):
        if current in override_by_date:
            override = override_by_date[current]
            hour, minute = parse_et_time_label(override["release_time"])
            details = (
                f"Holiday-adjusted natural gas storage release ({override['holiday']}) "
                f"at {override['release_time']} ET."
            )
            events.append(
                make_event(
                    event_date=current,
                    source="EIA",
                    title="Weekly Natural Gas Storage Report",
                    category="commodity",
                    region="US",
                    market="natural gas",
                    details=details,
                    url=US_EVENT_LINKS["natgas"],
                    time_ist=to_ist_datetime(current, hour, minute),
                )
            )
            continue
        if current.weekday() == 3 and current.isocalendar()[:2] not in suppressed_weeks:
            events.append(
                make_event(
                    event_date=current,
                    source="EIA",
                    title="Weekly Natural Gas Storage Report",
                    category="commodity",
                    region="US",
                    market="natural gas",
                    details="Standard weekly natural gas storage release at 10:30 AM ET.",
                    url=US_EVENT_LINKS["natgas"],
                    time_ist=to_ist_datetime(current, 10, 30),
                )
            )
    return events


def build_weekly_jobless_claims_events(start_date: date, end_date: date) -> list[dict[str, Any]]:
    events = []
    for current in iter_dates(start_date, end_date):
        if current.weekday() != 3:
            continue
        events.append(
            make_event(
                event_date=current,
                source="U.S. DOL",
                title="Unemployment Insurance Weekly Claims",
                category="macro",
                region="US",
                market="rates / equities / dollar",
                details="Standard weekly jobless claims release at 8:30 AM ET. Verify holiday-week timing if the U.S. calendar shifts.",
                url=US_EVENT_LINKS["jobless"],
                time_ist=to_ist_datetime(current, 8, 30),
            )
        )
    return events


def first_friday(year: int, month: int) -> date:
    current = date(year, month, 1)
    while current.weekday() != 4:
        current += timedelta(days=1)
    return current


def build_nfp_events(start_date: date, end_date: date) -> list[dict[str, Any]]:
    events = []
    cursor = date(start_date.year, start_date.month, 1)
    while cursor <= end_date:
        release_date = first_friday(cursor.year, cursor.month)
        if start_date <= release_date <= end_date:
            events.append(
                make_event(
                    event_date=release_date,
                    source="BLS",
                    title="Employment Situation / Nonfarm Payrolls",
                    category="macro",
                    region="US",
                    market="rates / equities / dollar / commodities",
                    details="First-Friday U.S. jobs report at 8:30 AM ET.",
                    url=US_EVENT_LINKS["nfp"],
                    time_ist=to_ist_datetime(release_date, 8, 30),
                )
            )
        cursor = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return events


def fetch_fomc_events(start_date: date, end_date: date) -> list[dict[str, Any]]:
    response = requests.get(US_EVENT_LINKS["fomc"], headers=HTTP_HEADERS, timeout=20)
    response.raise_for_status()
    text = strip_html(response.text)
    events = []

    for year_match in re.finditer(r"(\d{4}) FOMC Meetings (.*?)(?= \d{4} FOMC Meetings | Future Year:|Last Update:|$)", text):
        year = int(year_match.group(1))
        block = year_match.group(2)
        month_pattern = (
            r"(January|February|March|April|Apr/May|May|June|July|August|September|October|November|December)\s+"
            r"(\d{1,2}(?:-\d{1,2})?(?: and [A-Za-z]+ \d{1,2})?\*?)"
            r"(?= Statement:| Implementation Note| Press Conference| Projection Materials| "
            r"(?:January|February|March|April|Apr/May|May|June|July|August|September|October|November|December)\b|"
            r" \* Meeting associated|$)"
        )
        for month_name, day_block in re.findall(month_pattern, block):
            clean_block = day_block.replace("*", "")
            if "and" in clean_block and month_name == "September":
                left, right = [segment.strip() for segment in clean_block.split("and", 1)]
                left_start, left_end = [int(x) for x in left.split("-")]
                right_month, right_day = right.split()
                event_start = date(year, 9, left_start)
                event_end = date(year, datetime.strptime(right_month, "%B").month, int(right_day))
            else:
                month_label = "April" if month_name == "Apr/May" else month_name
                month_number = datetime.strptime(month_label, "%B").month
                if "-" in clean_block:
                    start_day, end_day = [int(x) for x in clean_block.split("-")]
                else:
                    start_day = end_day = int(clean_block)
                if month_name == "Apr/May":
                    event_start = date(year, 4, start_day)
                    event_end = date(year, 5, end_day)
                else:
                    event_start = date(year, month_number, start_day)
                    event_end = date(year, month_number, end_day)

            if event_end < start_date or event_start > end_date:
                continue
            statement_dt = to_ist_datetime(event_end, 14, 0)
            details = (
                f"Meeting window {event_start.isoformat()} to {event_end.isoformat()}; "
                f"statement typically lands at 2:00 PM ET on the final day."
            )
            events.append(
                make_event(
                    event_date=event_start,
                    window_end=event_end,
                    source="Federal Reserve",
                    title="FOMC Meeting",
                    category="central_bank",
                    region="US",
                    market="rates / equities / dollar / commodities",
                    details=details,
                    url=US_EVENT_LINKS["fomc"],
                    time_ist=statement_dt,
                )
            )
    return events


def fetch_rbi_policy_events(start_date: date, end_date: date) -> list[dict[str, Any]]:
    response = requests.get(US_EVENT_LINKS["rbi_schedule"], headers=HTTP_HEADERS, timeout=20)
    response.raise_for_status()
    text = strip_html(response.text)
    match = re.search(
        r"Dates of Meetings of Monetary Policy Committee for 2025-26 (.*?)(?: II\. Regulation |$)",
        text,
        flags=re.I,
    )
    if not match:
        return []

    block = match.group(1)
    events = []
    for month_segment in re.finditer(
        r"(?:\d+(?:st|nd|rd|th)\s+)?([A-Za-z]+)\s+(\d{1,2})(?:-(\d{1,2}))?(?: and ([A-Za-z]+) (\d{1,2}))?,\s*(\d{4})",
        block,
    ):
        start_month = datetime.strptime(month_segment.group(1), "%B").month
        start_day = int(month_segment.group(2))
        end_day = int(month_segment.group(3) or month_segment.group(2))
        year = int(month_segment.group(6))
        event_start = date(year, start_month, start_day)
        if month_segment.group(4):
            end_month = datetime.strptime(month_segment.group(4), "%B").month
            event_end = date(year, end_month, int(month_segment.group(5)))
        else:
            event_end = date(year, start_month, end_day)
        if event_end < start_date or event_start > end_date:
            continue
        events.append(
            make_event(
                event_date=event_start,
                window_end=event_end,
                source="RBI",
                title="Monetary Policy Committee Meeting",
                category="central_bank",
                region="India",
                market="rates / banks / rupee / index",
                details=f"Meeting window {event_start.isoformat()} to {event_end.isoformat()}; policy decision day is the final day.",
                url=US_EVENT_LINKS["rbi_schedule"],
            )
        )
    return events


def fetch_nse_earnings_watch(start_date: date, end_date: date, watchlist_symbols: Iterable[str]) -> dict[str, Any]:
    session = create_nse_session()
    response = session.get(
        "https://www.nseindia.com/api/corporate-board-meetings",
        params={"index": "equities"},
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json()
    watchlist = {normalized_text(symbol).upper() for symbol in watchlist_symbols if symbol}

    relevant = []
    for row in rows:
        purpose = normalized_text(row.get("bm_purpose"))
        meeting_date_raw = row.get("bm_date")
        if not meeting_date_raw:
            continue
        meeting_date = datetime.strptime(meeting_date_raw, "%d-%b-%Y").date()
        if not (start_date <= meeting_date <= end_date):
            continue
        purpose_lower = purpose.lower()
        if not any(term in purpose_lower for term in EARNINGS_PURPOSE_TERMS):
            continue
        record = {
            "symbol": normalized_text(row.get("bm_symbol")).upper(),
            "company_name": normalized_text(row.get("sm_name")),
            "meeting_date": meeting_date.isoformat(),
            "industry": normalized_text(row.get("sm_indusrty")),
            "purpose": purpose,
            "details": normalized_text(row.get("bm_desc")),
            "attachment": row.get("attachment"),
        }
        relevant.append(record)

    relevant.sort(key=lambda item: (item["meeting_date"], item["symbol"]))
    watchlist_hits = [item for item in relevant if item["symbol"] in watchlist]
    return {
        "watchlist_hits": watchlist_hits,
        "all_relevant": relevant[:20],
    }


def fetch_bea_events(start_date: date, end_date: date) -> list[dict[str, Any]]:
    response = requests.get("https://www.bea.gov/news/schedule", headers=HTTP_HEADERS, timeout=20)
    response.raise_for_status()
    events = []
    current_year = date.today().year

    for row_html in re.findall(r"<tr[^>]*>(.*?)</tr>", response.text, flags=re.S | re.I):
        text = strip_html(row_html)
        match = re.match(
            r"^(?P<month>[A-Za-z]+)\s+(?P<day>\d{1,2})\s+(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s+(?P<kind>[A-Za-z]+)\s+(?P<title>.+)$",
            text,
        )
        if not match:
            continue
        try:
            event_date = datetime.strptime(
                f"{match.group('month')} {match.group('day')} {current_year}", "%B %d %Y"
            ).date()
        except ValueError:
            continue
        if not (start_date <= event_date <= end_date):
            continue
        time_label = match.group("time")
        hour, minute = parse_et_time_label(time_label)
        events.append(
            make_event(
                event_date=event_date,
                source="BEA",
                title=match.group("title"),
                category="macro",
                region="US",
                market="rates / dollar / global risk",
                details=f"{match.group('kind')} release at {time_label} ET.",
                url=US_EVENT_LINKS["bea"],
                time_ist=to_ist_datetime(event_date, hour, minute),
            )
        )
    return events


def _fed_month_url(target: date) -> str:
    return f"https://www.federalreserve.gov/newsevents/{target.year}-{target.strftime('%B').lower()}.htm"


def fetch_fed_events(start_date: date, end_date: date) -> list[dict[str, Any]]:
    events = []
    month_cursor = date(start_date.year, start_date.month, 1)
    seen_urls: set[str] = set()

    while month_cursor <= end_date:
        url = _fed_month_url(month_cursor)
        if url in seen_urls:
            break
        seen_urls.add(url)
        response = requests.get(url, headers=HTTP_HEADERS, timeout=20)
        if response.status_code != 200:
            month_cursor = (month_cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
            continue
        for panel_html in re.findall(
            r'<div[^>]*class="[^"]*panel-body[^"]*"[^>]*>(.*?)</div>',
            response.text,
            flags=re.S | re.I,
        ):
            text = strip_html(panel_html)
            if not text:
                continue
            day_match = re.search(r"\b(\d{1,2})\s*$", text)
            if not day_match:
                continue
            day_value = int(day_match.group(1))
            try:
                event_date = date(month_cursor.year, month_cursor.month, day_value)
            except ValueError:
                continue
            if not (start_date <= event_date <= end_date):
                continue
            title = normalized_text(text[: day_match.start()].strip())
            if not title:
                continue
            events.append(
                make_event(
                    event_date=event_date,
                    source="Federal Reserve",
                    title=title,
                    category="central_bank",
                    region="US",
                    market="rates / equities / dollar",
                    details="Federal Reserve scheduled communication.",
                    url=url,
                )
            )
        month_cursor = (month_cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
    return events


def fetch_nse_holidays(start_date: date, end_date: date) -> list[dict[str, Any]]:
    response = requests.get(
        "https://www.nseindia.com/api/holiday-master",
        params={"type": "trading"},
        headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for segment in ("CM", "FO", "COM"):
        for item in payload.get(segment, []):
            holiday_date = datetime.strptime(item["tradingDate"], "%d-%b-%Y").date()
            if not (start_date <= holiday_date <= end_date):
                continue
            key = (holiday_date.isoformat(), item["description"])
            grouped.setdefault(
                key,
                {
                    "date": holiday_date.isoformat(),
                    "description": item["description"],
                    "weekday": item.get("weekDay"),
                    "segments": [],
                    "morning_session": item.get("morning_session"),
                    "evening_session": item.get("evening_session"),
                },
            )
            grouped[key]["segments"].append(segment)
    return sorted(grouped.values(), key=lambda item: item["date"])


def build_weekly_watchlist_symbols(fno_data: Optional[dict[str, Any]]) -> list[str]:
    if not fno_data:
        return []
    symbols: list[str] = []
    for bucket in ("bullish_stocks", "bearish_stocks"):
        for row in fno_data.get(bucket, [])[:8]:
            symbol = normalized_text(row.get("symbol"))
            if symbol and symbol not in symbols:
                symbols.append(symbol)
    return symbols


def build_event_risk_package(start_date: date, end_date: date, watchlist_symbols: Iterable[str]) -> dict[str, Any]:
    scheduled = []
    scheduled.extend(get_msci_review_events(start_date, end_date))
    scheduled.extend(fetch_bea_events(start_date, end_date))
    scheduled.extend(build_eia_crude_events(start_date, end_date))
    scheduled.extend(build_eia_natgas_events(start_date, end_date))
    scheduled.extend(build_weekly_jobless_claims_events(start_date, end_date))
    scheduled.extend(build_nfp_events(start_date, end_date))
    scheduled.extend(fetch_fomc_events(start_date, end_date))
    try:
        scheduled.extend(fetch_rbi_policy_events(start_date, end_date))
    except Exception as exc:
        logger.warning("RBI policy event fetch failed: %s", exc)

    holidays = fetch_nse_holidays(start_date, end_date)
    earnings_watch = {"watchlist_hits": [], "all_relevant": []}
    try:
        earnings_watch = fetch_nse_earnings_watch(start_date, end_date, watchlist_symbols)
    except Exception as exc:
        logger.warning("NSE earnings watch fetch failed: %s", exc)

    return {
        "scheduled": sort_events(scheduled),
        "holidays": holidays,
        "earnings_watch": earnings_watch,
        "special_risk_flags": build_special_risk_flags(sort_events(scheduled)),
    }


def build_special_risk_flags(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flags: list[dict[str, Any]] = []
    for item in events:
        if item.get("event_code") != "MSCI_REBALANCE_CLOSE":
            continue
        flags.append(
            {
                "type": "passive_flow",
                "date": item.get("date"),
                "title": item.get("title"),
                "message": "MSCI implementation close-flow risk: no fresh index balancing legs after 14:30 IST.",
                "source": item.get("source"),
                "url": item.get("url"),
            }
        )
    return flags


def build_week_ahead_plan(
    indices: dict[str, Any],
    fno_data: Optional[dict[str, Any]],
    commodities: dict[str, Any],
    next_week_event_package: dict[str, Any],
    trade_review: dict[str, Any],
    prediction_review: dict[str, Any],
) -> dict[str, Any]:
    bullish_indices = [name for name, data in indices.items() if str(data.get("bias", "")).startswith("bullish")]
    bearish_indices = [name for name, data in indices.items() if str(data.get("bias", "")).startswith("bearish")]

    strong_sectors = (fno_data or {}).get("strong_sectors", [])[:5]
    weak_sectors = (fno_data or {}).get("weak_sectors", [])[:5]
    bullish_stocks = [
        {
            "symbol": stock["symbol"],
            "sector": stock["sector"],
            "score": stock["score"],
            "price": stock["price"],
        }
        for stock in (fno_data or {}).get("bullish_stocks", [])[:5]
    ]
    bearish_stocks = [
        {
            "symbol": stock["symbol"],
            "sector": stock["sector"],
            "score": stock["score"],
            "price": stock["price"],
        }
        for stock in (fno_data or {}).get("bearish_stocks", [])[:5]
    ]

    bullish_commodities = [
        item for item in commodities.values() if item.get("bias") == "bullish" and item.get("probability", 0) >= 60
    ]
    bearish_commodities = [
        item for item in commodities.values() if item.get("bias") == "bearish" and item.get("probability", 0) >= 60
    ]
    bullish_commodities.sort(key=lambda item: item.get("probability", 0), reverse=True)
    bearish_commodities.sort(key=lambda item: item.get("probability", 0), reverse=True)

    risk_focus = []
    if trade_review.get("rule_signals", {}).get("averaging_mentions", 0):
        risk_focus.append("Averaging behaviour was visible in the weekly journal notes; next week should stay in reduce-or-exit mode after invalidation.")
    if trade_review.get("rule_signals", {}).get("trade_days_over_4_rows", 0):
        risk_focus.append("There were overtrading days this week; keep the four-idea daily cap front and center.")
    if prediction_review.get("gaps"):
        weakest_signal = prediction_review["gaps"][0]
        risk_focus.append(
            f"Weakest prediction family last week was {weakest_signal['signal_family']} "
            f"({weakest_signal['hit_rate']:.1f}% hit rate on {weakest_signal['evaluated']} evaluated calls)."
        )

    focus_lines = []
    if bullish_indices and not bearish_indices:
        focus_lines.append(f"Higher timeframe index backdrop leans bullish across {', '.join(bullish_indices)}.")
    elif bearish_indices and not bullish_indices:
        focus_lines.append(f"Higher timeframe index backdrop leans bearish across {', '.join(bearish_indices)}.")
    else:
        focus_lines.append("Index backdrop is mixed, so next week should emphasize selective stock and structure-specific setups over broad directional aggression.")

    if strong_sectors:
        focus_lines.append("Strong weekly stock focus sectors: " + ", ".join(strong_sectors) + ".")
    if weak_sectors:
        focus_lines.append("Weak weekly stock pockets to monitor: " + ", ".join(weak_sectors) + ".")
    if bullish_commodities:
        focus_lines.append(
            "Commodity strength watchlist: "
            + ", ".join(f"{item['commodity']} ({item['probability']:.0f}%)" for item in bullish_commodities[:4])
            + "."
        )
    if bearish_commodities:
        focus_lines.append(
            "Commodity weakness watchlist: "
            + ", ".join(f"{item['commodity']} ({item['probability']:.0f}%)" for item in bearish_commodities[:4])
            + "."
        )
    next_week_events = next_week_event_package.get("scheduled", [])
    next_week_holidays = next_week_event_package.get("holidays", [])
    earnings_watch = next_week_event_package.get("earnings_watch", {})
    watchlist_earnings = earnings_watch.get("watchlist_hits", [])
    special_risk_flags = next_week_event_package.get("special_risk_flags", [])

    if next_week_events:
        focus_lines.append("Next week has scheduled macro risk; keep size lighter around release windows and avoid initiating fresh trades just ahead of those events.")
    if special_risk_flags:
        focus_lines.append(
            "MSCI passive-flow day is on the calendar next week; avoid fresh index balancing legs after 14:30 IST and keep late-day F&O lighter."
        )
    if next_week_holidays:
        focus_lines.append("There is at least one market holiday / session variation next week, so expiry and settlement assumptions need a quick re-check before trading.")
    if watchlist_earnings:
        focus_lines.append(
            "Watchlist names with earnings / board-meeting risk next week: "
            + ", ".join(f"{item['symbol']} ({item['meeting_date']})" for item in watchlist_earnings[:6])
            + ". Avoid casual intraday or positional exposure unless it is an explicit earnings setup."
        )

    next_week_by_market: dict[str, int] = defaultdict(int)
    for item in next_week_events:
        market = normalized_text(item.get("market") or item.get("category") or "macro")
        next_week_by_market[market] += 1
    if next_week_by_market:
        busiest = sorted(next_week_by_market.items(), key=lambda kv: kv[1], reverse=True)[:3]
        focus_lines.append(
            "Event-heavy pockets next week: " + ", ".join(f"{name} ({count})" for name, count in busiest) + "."
        )

    weekday_calendar = [
        {"weekday": "Monday", "focus": "F&O core; commodity satellite only if structure is A+"},
        {"weekday": "Tuesday", "focus": "F&O core; expiry structure if the chain sets up cleanly"},
        {"weekday": "Wednesday", "focus": "F&O core; keep trade count tight"},
        {"weekday": "Thursday", "focus": "Index / expiry structure day; avoid turning a good morning into an afternoon repair campaign"},
        {"weekday": "Friday", "focus": "Selective commodity intraday first; F&O only if the setup is unusually clean"},
    ]

    return {
        "focus_lines": focus_lines,
        "weekday_calendar": weekday_calendar,
        "top_bullish_stocks": bullish_stocks,
        "top_bearish_stocks": bearish_stocks,
        "bullish_stock_buckets": build_stock_style_buckets(bullish_stocks, direction="bullish"),
        "bearish_stock_buckets": build_stock_style_buckets(bearish_stocks, direction="bearish"),
        "top_bullish_commodities": bullish_commodities[:5],
        "top_bearish_commodities": bearish_commodities[:5],
        "commodity_buckets": build_commodity_style_buckets(commodities),
        "watchlist_earnings": watchlist_earnings[:10],
        "risk_focus": risk_focus,
    }


def build_stock_style_buckets(stocks: list[dict[str, Any]], *, direction: str) -> dict[str, list[dict[str, Any]]]:
    positional: list[dict[str, Any]] = []
    intraday: list[dict[str, Any]] = []
    specialist: list[dict[str, Any]] = []

    for item in stocks:
        symbol = normalized_text(item.get("symbol"))
        sector = normalized_text(item.get("sector")).upper()
        price = float(item.get("price") or 0)

        if direction == "bullish":
            if sector in {"BANKING", "ENERGY", "PHARMA"}:
                positional.append(item)
            elif sector in {"METAL", "AUTO"}:
                intraday.append(item)
            else:
                intraday.append(item)
            continue

        # bearish
        if symbol == "MRF" or price >= 50000:
            specialist.append(item)
        elif sector in {"IT", "FMCG"}:
            positional.append(item)
        elif sector in {"AUTO", "METAL", "ENERGY"}:
            intraday.append(item)
        else:
            intraday.append(item)

    return {
        "positional": positional[:5],
        "intraday": intraday[:5],
        "specialist": specialist[:5],
    }


def build_commodity_style_buckets(commodities: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    positional: list[dict[str, Any]] = []
    intraday: list[dict[str, Any]] = []
    avoid: list[dict[str, Any]] = []

    for name, item in commodities.items():
        payload = {
            "commodity": name,
            "bias": item.get("bias"),
            "trend": item.get("trend"),
            "structure": item.get("structure"),
            "probability": item.get("probability"),
            "trade_type": item.get("trade_type"),
        }
        bias = str(item.get("bias") or "").lower()
        trend = str(item.get("trend") or "").lower()
        structure = str(item.get("structure") or "").lower()
        probability = float(item.get("probability") or 0)

        if name in {"NATURALGAS", "ALUMINIUM"} and probability >= 70:
            positional.append(payload)
        elif name in {"CRUDEOIL", "GOLD", "GOLDM"} and probability >= 75:
            intraday.append(payload)
        elif name in {"SILVER", "SILVERM", "ZINC", "ZINCMINI", "COPPER", "LEAD", "NICKEL"}:
            avoid.append(payload)
        elif bias in {"bullish", "bearish"} and trend in {"uptrend", "downtrend"} and structure in {"trending", "breakout", "breakdown"}:
            intraday.append(payload)
        else:
            avoid.append(payload)

    return {
        "positional_quality": positional[:5],
        "intraday_only": intraday[:8],
        "avoid_or_mixed": avoid[:8],
    }


def render_report(payload: dict[str, Any]) -> str:
    lines: list[str] = []
    week_start = payload["week_start"]
    week_end = payload["week_end"]
    next_week_start = payload["next_week_start"]
    next_week_end = payload["next_week_end"]

    lines.append("=" * 108)
    lines.append("WEEKLY REVIEW AND WEEK-AHEAD PLAN")
    lines.append(f"Generated: {payload['generated_at']}")
    lines.append(f"Review Week: {week_start} to {week_end}")
    lines.append(f"Next Week:   {next_week_start} to {next_week_end}")
    lines.append("=" * 108)

    lines.append("\n1. HIGHER-TIMEFRAME INDEX CONTEXT")
    lines.append("-" * 108)
    for symbol, summary in payload["market_context"]["indices"].items():
        lines.append(
            f"{symbol:<10} | Bias {summary['bias']:<22} | Spot {summary['current']:>10.2f} | "
            f"1W {format_pct(summary['week_return_pct']):>9} | 1M {format_pct(summary['month_return_pct']):>9} | "
            f"20D support/res {summary['support_20d']:.2f}/{summary['resistance_20d']:.2f}"
        )

    lines.append("\n2. GLOBAL BACKDROP")
    lines.append("-" * 108)
    global_data = payload["market_context"].get("global", {})
    lines.append(f"Overall bias: {global_data.get('overall_bias', 'N/A')}")
    bullish = global_data.get("bullish_signals", [])[:5]
    bearish = global_data.get("bearish_signals", [])[:5]
    if bullish:
        lines.append("Bullish signals: " + " | ".join(bullish))
    if bearish:
        lines.append("Bearish signals: " + " | ".join(bearish))

    lines.append("\n3. WEEKLY STOCK / F&O SCAN")
    lines.append("-" * 108)
    fno_data = payload["market_context"].get("fno", {})
    lines.append("Strong sectors: " + ", ".join(fno_data.get("strong_sectors", [])[:6] or ["N/A"]))
    lines.append("Weak sectors:   " + ", ".join(fno_data.get("weak_sectors", [])[:6] or ["N/A"]))
    lines.append("Top bullish names:")
    for stock in fno_data.get("bullish_stocks", [])[:5]:
        lines.append(
            f"  - {stock['symbol']} | {stock['sector']} | score {stock['score']} | "
            f"price {stock['price']:.2f} | RSI {stock['rsi_14']:.1f} | RS {stock['rs_vs_nifty']:+.1f}"
        )
    lines.append("Top bearish names:")
    for stock in fno_data.get("bearish_stocks", [])[:5]:
        lines.append(
            f"  - {stock['symbol']} | {stock['sector']} | score {stock['score']} | "
            f"price {stock['price']:.2f} | RSI {stock['rsi_14']:.1f} | RS {stock['rs_vs_nifty']:+.1f}"
        )

    lines.append("\n4. WEEKLY COMMODITY STRUCTURE")
    lines.append("-" * 108)
    for commodity, summary in payload["market_context"].get("commodities", {}).items():
        lines.append(
            f"{commodity:<12} | Bias {summary['bias']:<8} | Weekly {summary['weekly_bias']:<7} | "
            f"Trend {summary['trend']:<10} | Structure {summary['structure']:<10} | "
            f"1W {format_pct(summary['week_return_pct']):>9} | 1M {format_pct(summary['month_return_pct']):>9}"
        )

    lines.append("\n5. PAST WEEK TRADE REVIEW")
    lines.append("-" * 108)
    trade_review = payload["trade_review"]
    lines.append(
        f"Rows: {trade_review['total_rows']} | Closed: {trade_review['closed_rows']} | Open: {trade_review['open_rows']} | "
        f"Realized net: {format_money(trade_review['net_realized_pnl'])} | Fees: {format_money(trade_review['total_fees'])}"
    )
    lines.append("By account:")
    for item in trade_review.get("by_account", []):
        lines.append(
            f"  - {item['account']}: {format_money(item['net_pnl'])} | rows {item['rows']} | "
            f"win rate {item['win_rate']:.1f}% | mean {format_money(item['mean_pnl'])}"
        )
    lines.append("By segment:")
    for item in trade_review.get("by_segment", []):
        lines.append(
            f"  - {item['segment']}: {format_money(item['net_pnl'])} | rows {item['rows']} | "
            f"win rate {item['win_rate']:.1f}%"
        )
    lines.append("Top winners:")
    for item in trade_review.get("top_winners", []):
        lines.append(f"  - {item['account']} {item['symbol']}: {format_money(item['net_pnl'])}")
    lines.append("Top losers:")
    for item in trade_review.get("top_losers", []):
        lines.append(f"  - {item['account']} {item['symbol']}: {format_money(item['net_pnl'])}")
    signals = trade_review.get("rule_signals", {})
    if signals:
        parts = [
            f"averaging mentions {signals.get('averaging_mentions', 0)}",
            f"impulsive mentions {signals.get('impulsive_mentions', 0)}",
            f"days over 4 rows {signals.get('trade_days_over_4_rows', 0)}",
        ]
        if signals.get("plan_misses") is not None:
            parts.append(f"plan misses {signals.get('plan_misses', 0)}")
        lines.append("Rule signals: " + ", ".join(parts))

    lines.append("\n5B. BROKER VS NOTION RECONCILIATION")
    lines.append("-" * 108)
    reconciliation = payload.get("journal_reconciliation") or {}
    if reconciliation:
        recon_summary = reconciliation.get("summary") or {}
        lines.append(f"Status: {str(reconciliation.get('status') or 'unknown').upper()}")
        if recon_summary:
            lines.append(
                f"Broker fills: {recon_summary.get('broker_fills', 0)} | "
                f"Notion rows: {recon_summary.get('notion_rows', 0)} | "
                f"Source-ID matches: {recon_summary.get('matched_by_source_id', 0)} | "
                f"Missing fills: {recon_summary.get('missing_broker_fills', 0)} | "
                f"Keyless rows: {recon_summary.get('notion_keyless_rows', 0)}"
            )
        if reconciliation.get("by_account"):
            for item in reconciliation["by_account"]:
                lines.append(
                    f"  - {item['account']}: broker fills {item['broker_fills']} | "
                    f"notion rows {item['notion_rows']} | missing {item['missing_broker_fills']}"
                )
        if reconciliation.get("missing_broker_fills"):
            lines.append("Missing fills requiring explanation:")
            for item in reconciliation["missing_broker_fills"][:8]:
                lines.append(
                    f"  - {item['account']} {item['trade_date']} {item['time']} "
                    f"{item['side']} {item['quantity']} {item['symbol']} @ {item['price']}"
                )
        if reconciliation.get("error"):
            lines.append(f"Reconciliation error: {str(reconciliation['error'])[:500]}")
        if reconciliation.get("latest_text_path"):
            lines.append(f"Full reconciliation report: {reconciliation['latest_text_path']}")
    else:
        lines.append("Reconciliation was not run for this report.")

    lines.append("\n6. PAST WEEK PREDICTION REVIEW")
    lines.append("-" * 108)
    prediction_review = payload["prediction_review"]
    lines.append(
        f"Predictions: {prediction_review['total_predictions']} | Evaluated: {prediction_review['evaluated_predictions']} | "
        f"Overall hit rate: {prediction_review['overall_hit_rate'] if prediction_review['overall_hit_rate'] is not None else 'N/A'}"
    )
    lines.append("By asset class:")
    for item in prediction_review.get("by_asset_class", []):
        lines.append(
            f"  - {item['asset_class']}: {item['hit_rate']:.1f}% hit rate on {item['evaluated']} evaluated calls"
        )
    lines.append("Prediction strengths:")
    for item in prediction_review.get("strengths", []):
        lines.append(
            f"  - {item['signal_family']}: {item['hit_rate']:.1f}% on {item['evaluated']} evaluations"
        )
    lines.append("Prediction gaps:")
    for item in prediction_review.get("gaps", []):
        lines.append(
            f"  - {item['signal_family']}: {item['hit_rate']:.1f}% on {item['evaluated']} evaluations"
        )

    lines.append("\n7. TOP 5 DISRUPTIVE NEWS / MARKET EVENTS FROM THE WEEK")
    lines.append("-" * 108)
    for item in payload["weekly_events"].get("top_news", []):
        score = item.get("priority_score")
        score_text = f" | score {score}" if score is not None else ""
        lines.append(
            f"  - [{item['category']}] {item['title']} ({item.get('source') or 'source n/a'}{score_text})"
        )

    lines.append("\n8. SCHEDULED EVENT RISK - LAST WEEK")
    lines.append("-" * 108)
    last_week_events = payload["weekly_events"].get("last_week_event_package", {}).get("scheduled", [])
    if last_week_events:
        for item in last_week_events[:14]:
            window = f" to {item['window_end']}" if item.get("window_end") else ""
            time_text = f" {item['time_ist']}" if item.get("time_ist") else ""
            detail = f" | {item['details']}" if item.get("details") else ""
            lines.append(
                f"  - {item['date']}{window}{time_text} | {item['source']} [{item.get('market') or item.get('category')}]"
                f": {item['title']}{detail}"
            )
    else:
        lines.append("  - No high-signal scheduled event-risk items captured in the current source set.")

    lines.append("\n9. EVENT RISK / HOLIDAYS / EARNINGS - NEXT WEEK")
    lines.append("-" * 108)
    next_week_package = payload["weekly_events"].get("next_week_event_package", {})
    next_week_events = next_week_package.get("scheduled", [])
    if next_week_events:
        lines.append("Upcoming scheduled macro / commodity / policy events:")
        for item in next_week_events[:16]:
            window = f" to {item['window_end']}" if item.get("window_end") else ""
            time_text = f" {item['time_ist']}" if item.get("time_ist") else ""
            detail = f" | {item['details']}" if item.get("details") else ""
            lines.append(
                f"  - {item['date']}{window}{time_text} | {item['source']} [{item.get('market') or item.get('category')}]"
                f": {item['title']}{detail}"
            )
    else:
        lines.append("  - No high-signal scheduled event-risk items captured in the current source set.")
    holidays = next_week_package.get("holidays", [])
    if holidays:
        lines.append("Upcoming market holidays / session changes:")
        for item in holidays:
            sessions = []
            if item.get("morning_session"):
                sessions.append(f"morning {item['morning_session']}")
            if item.get("evening_session"):
                sessions.append(f"evening {item['evening_session']}")
            session_text = f" ({', '.join(sessions)})" if sessions else ""
            lines.append(
                f"  - {item['date']} | {item['description']} | segments {', '.join(item['segments'])}{session_text}"
            )
    else:
        lines.append("No NSE trading / commodity holiday changes detected for next week.")

    special_flags = next_week_package.get("special_risk_flags", [])
    if special_flags:
        lines.append("Special market-structure risk days:")
        for item in special_flags:
            lines.append(f"  - {item['date']} | {item['message']}")

    earnings_watch = next_week_package.get("earnings_watch", {})
    watchlist_hits = earnings_watch.get("watchlist_hits", [])
    if watchlist_hits:
        lines.append("Weekly stock watchlist names with earnings / board-meeting risk:")
        for item in watchlist_hits[:12]:
            detail = f" | {item['details']}" if item.get("details") else ""
            lines.append(
                f"  - {item['meeting_date']} | {item['symbol']} | {item['company_name']} | {item['purpose']}{detail}"
            )
    else:
        broader_hits = earnings_watch.get("all_relevant", [])
        if broader_hits:
            lines.append("Broader F&O / equity board-meeting results risk next week:")
            for item in broader_hits[:10]:
                lines.append(
                    f"  - {item['meeting_date']} | {item['symbol']} | {item['company_name']} | {item['purpose']}"
                )

    lines.append("\n10. WHAT TO LOOK FOR NEXT WEEK")
    lines.append("-" * 108)
    week_ahead = payload["week_ahead_plan"]
    for line in week_ahead.get("focus_lines", []):
        lines.append(f"  - {line}")
    if week_ahead.get("risk_focus"):
        lines.append("Risk focus:")
        for line in week_ahead["risk_focus"]:
            lines.append(f"  - {line}")
    bullish_buckets = week_ahead.get("bullish_stock_buckets") or {}
    bearish_buckets = week_ahead.get("bearish_stock_buckets") or {}
    if bullish_buckets:
        lines.append("Higher-timeframe bullish stock buckets:")
        if bullish_buckets.get("positional"):
            lines.append("  Positional / stock-option candidates:")
            for item in bullish_buckets["positional"][:10]:
                lines.append(
                    f"    - {item['symbol']} | {item['sector']} | score {item['score']} | price {item['price']}"
                )
        if bullish_buckets.get("intraday"):
            lines.append("  Intraday continuation candidates:")
            for item in bullish_buckets["intraday"][:10]:
                lines.append(
                    f"    - {item['symbol']} | {item['sector']} | score {item['score']} | price {item['price']}"
                )
    if bearish_buckets:
        lines.append("Higher-timeframe bearish stock buckets:")
        if bearish_buckets.get("positional"):
            lines.append("  Positional bearish / spread candidates:")
            for item in bearish_buckets["positional"][:10]:
                lines.append(
                    f"    - {item['symbol']} | {item['sector']} | score {item['score']} | price {item['price']}"
                )
        if bearish_buckets.get("intraday"):
            lines.append("  Intraday short candidates:")
            for item in bearish_buckets["intraday"][:10]:
                lines.append(
                    f"    - {item['symbol']} | {item['sector']} | score {item['score']} | price {item['price']}"
                )
        if bearish_buckets.get("specialist"):
            lines.append("  Specialist / skip-unless-specific-setup candidates:")
            for item in bearish_buckets["specialist"][:10]:
                lines.append(
                    f"    - {item['symbol']} | {item['sector']} | score {item['score']} | price {item['price']}"
                )
    commodity_buckets = week_ahead.get("commodity_buckets") or {}
    if commodity_buckets:
        lines.append("Commodity buckets:")
        if commodity_buckets.get("positional_quality"):
            lines.append("  Positional-quality commodities:")
            for item in commodity_buckets["positional_quality"][:10]:
                lines.append(
                    f"    - {item['commodity']} | {item['bias']} | {item['structure']} | probability {item['probability']}"
                )
        if commodity_buckets.get("intraday_only"):
            lines.append("  Intraday-only commodities:")
            for item in commodity_buckets["intraday_only"][:10]:
                lines.append(
                    f"    - {item['commodity']} | {item['bias']} | {item['structure']} | probability {item['probability']}"
                )
        if commodity_buckets.get("avoid_or_mixed"):
            lines.append("  Avoid / mixed commodities:")
            for item in commodity_buckets["avoid_or_mixed"][:10]:
                lines.append(
                    f"    - {item['commodity']} | {item['bias']} | {item['structure']} | probability {item['probability']}"
                )
    if week_ahead.get("watchlist_earnings"):
        lines.append("Earnings / board-meeting watchlist:")
        for item in week_ahead["watchlist_earnings"][:10]:
            lines.append(f"  - {item['meeting_date']} | {item['symbol']} | {item['purpose']}")
    lines.append("Weekday operating calendar:")
    for item in week_ahead.get("weekday_calendar", []):
        lines.append(f"  - {item['weekday']}: {item['focus']}")

    return "\n".join(lines) + "\n"


def main() -> int:
    load_env()
    args = parse_args()
    started_at = datetime.now()

    try:
        week_end = datetime.strptime(args.week_ending, "%Y-%m-%d").date()
    except ValueError as exc:
        raise SystemExit(f"Invalid --week-ending value '{args.week_ending}': {exc}") from exc

    week_start = week_end - timedelta(days=4)
    next_week_start = week_end + timedelta(days=3)
    next_week_end = next_week_start + timedelta(days=4)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    logger.info("Running weekly review for %s to %s", week_start, week_end)

    run_times: dict[str, float] = {}

    start = timer.time()
    global_result = run_global_markets()
    run_times["global"] = timer.time() - start

    start = timer.time()
    fno_result = run_fno_scanner(mode="eod")
    run_times["fno"] = timer.time() - start

    start = timer.time()
    indices = analyze_weekly_indices()
    run_times["indices"] = timer.time() - start

    start = timer.time()
    commodities = analyze_weekly_commodities()
    run_times["commodities"] = timer.time() - start

    start = timer.time()
    notion_rows = fetch_weekly_notion_trades(week_start, week_end)
    trade_review = summarize_weekly_trades(notion_rows, week_start, week_end)
    run_times["notion_review"] = timer.time() - start

    start = timer.time()
    if args.no_reconciliation:
        journal_reconciliation = {"status": "skipped"}
    else:
        journal_reconciliation = run_weekly_journal_reconciliation(week_start, week_end)
    run_times["journal_reconciliation"] = timer.time() - start

    start = timer.time()
    prediction_rows = fetch_weekly_prediction_rows(week_start, week_end)
    prediction_review = summarize_prediction_review(prediction_rows)
    run_times["prediction_review"] = timer.time() - start

    watchlist_symbols = build_weekly_watchlist_symbols(fno_result.data if fno_result.success else None)
    top_news: list[dict[str, Any]] = []
    last_week_event_package: dict[str, Any] = {"scheduled": [], "holidays": [], "earnings_watch": {"watchlist_hits": [], "all_relevant": []}}
    next_week_event_package: dict[str, Any] = {"scheduled": [], "holidays": [], "earnings_watch": {"watchlist_hits": [], "all_relevant": []}}

    if not args.no_news:
        start = timer.time()
        try:
            top_news = fetch_top_weekly_news(limit=5)
            last_week_event_package = build_event_risk_package(week_start, week_end, watchlist_symbols)
            next_week_event_package = build_event_risk_package(next_week_start, next_week_end, watchlist_symbols)
        except Exception as exc:
            logger.warning("Weekly news/calendar fetch failed: %s", exc)
        run_times["news_calendar"] = timer.time() - start

    week_ahead_plan = build_week_ahead_plan(
        indices=indices,
        fno_data=fno_result.data if fno_result.success else None,
        commodities=commodities,
        next_week_event_package=next_week_event_package,
        trade_review=trade_review,
        prediction_review=prediction_review,
    )

    payload = {
        "generated_at": started_at.isoformat(),
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "next_week_start": next_week_start.isoformat(),
        "next_week_end": next_week_end.isoformat(),
        "output_dir": str(output_dir),
        "run_times": {key: round(value, 2) for key, value in run_times.items()},
        "market_context": {
            "global": global_result.data if global_result.success else {},
            "fno": fno_result.data if fno_result.success else {},
            "indices": indices,
            "commodities": commodities,
        },
        "trade_review": trade_review,
        "journal_reconciliation": journal_reconciliation,
        "prediction_review": prediction_review,
        "weekly_events": {
            "top_news": top_news,
            "last_week_event_package": last_week_event_package,
            "next_week_event_package": next_week_event_package,
        },
        "week_ahead_plan": week_ahead_plan,
    }

    rendered = render_report(payload)
    timestamp = started_at.strftime("%Y%m%d_%H%M")
    text_path = output_dir / f"weekly_review_and_plan_{timestamp}.txt"
    json_path = output_dir / f"weekly_review_and_plan_{timestamp}.json"
    latest_text_path = output_dir / "weekly_review_and_plan_latest.txt"
    latest_json_path = output_dir / "weekly_review_and_plan_latest.json"

    text_path.write_text(rendered, encoding="utf-8")
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    latest_text_path.write_text(rendered, encoding="utf-8")
    latest_json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    print(rendered)
    logger.info("Weekly review saved to %s", text_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
