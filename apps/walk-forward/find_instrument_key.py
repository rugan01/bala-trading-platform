"""
Find the current SILVERMIC front-month futures instrument key from Upstox.

Run from terminal:
    cd /path/to/bala-trading-platform/apps/walk-forward
    python find_instrument_key.py
"""

import gzip
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"

load_dotenv(DEFAULT_ENV_FILE)
ACCESS_TOKEN = os.getenv("UPSTOX_ACCESS_TOKEN", "")

if not ACCESS_TOKEN:
    print("ERROR: UPSTOX_ACCESS_TOKEN not found in .env")
    sys.exit(1)

TODAY = date.today()

# ── Download attempt order ────────────────────────────────────────────────────
# 1. Upstox market-quote MCX master (same logic as analyzer project)
# 2. Upstox authenticated API
# 3. Legacy CDN fallbacks
SOURCES = [
    {
        "label": "Upstox market-quote — MCX instruments",
        "url": "https://assets.upstox.com/market-quote/instruments/exchange/MCX.json.gz",
        "params": {},
        "headers": {"Accept": "application/json"},
        "is_json": False,  # gzip
        "data_key": None,
    },
    {
        "label": "Upstox API (authenticated)",
        "url": "https://api.upstox.com/v2/instruments",
        "params": {"exchange": "MCX_FO"},
        "headers": {
            "Authorization": f"Bearer {ACCESS_TOKEN}",
            "Accept": "application/json",
        },
        "is_json": True,
        "data_key": "data",   # response wraps list under .data
    },
    {
        "label": "Upstox CDN — MCX instruments",
        "url": "https://assets.upstox.com/market-assets/instruments/exchange/MCX.json.gz",
        "params": {},
        "headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Referer": "https://upstox.com/",
        },
        "is_json": False,  # gzip
        "data_key": None,
    },
    {
        "label": "Upstox CDN — complete instruments",
        "url": "https://assets.upstox.com/market-assets/instruments/v2/complete.json.gz",
        "params": {},
        "headers": {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Referer": "https://upstox.com/",
        },
        "is_json": False,
        "data_key": None,
    },
]


def download_instruments(source: dict) -> list | None:
    print(f"  Trying: {source['label']}")
    try:
        r = requests.get(
            source["url"],
            headers=source["headers"],
            params=source["params"],
            timeout=30,
        )
        print(f"  Status: {r.status_code}")
        if r.status_code != 200:
            return None

        if source["is_json"]:
            body = r.json()
            return body.get(source["data_key"], body) if source["data_key"] else body
        else:
            try:
                return json.loads(gzip.decompress(r.content))
            except Exception:
                return r.json()

    except Exception as e:
        print(f"  Error: {e}")
        return None


def find_silvermic(instruments: list) -> list:
    """Filter to SILVERMIC futures with expiry >= today, sorted by expiry ascending."""
    candidates = []
    for inst in instruments:
        exchange = (inst.get("segment") or inst.get("exchange") or "").upper()
        symbol   = (inst.get("tradingsymbol") or inst.get("trading_symbol") or "").upper()
        itype    = (inst.get("instrument_type") or "").upper()
        expiry_raw = inst.get("expiry")
        expiry_date, expiry = normalize_expiry(expiry_raw)

        if exchange not in ("MCX_FO", "MCX"):
            continue
        if "SILVERMIC" not in symbol:
            continue
        if itype not in ("FUT", "FUTCOM"):
            continue
        if not expiry_date:
            continue

        if expiry_date < TODAY:
            continue

        token = inst.get("instrument_token") or inst.get("instrument_key") or ""
        key = str(token) if "|" in str(token) else f"MCX_FO|{token}"

        candidates.append({
            "key": key,
            "symbol": symbol,
            "expiry": expiry,
            "expiry_date": expiry_date,
        })

    return sorted(candidates, key=lambda x: x["expiry_date"])


def normalize_expiry(raw_expiry) -> tuple[date | None, str]:
    if raw_expiry in (None, ""):
        return None, ""

    if isinstance(raw_expiry, (int, float)):
        ts = float(raw_expiry)
        if ts > 10_000_000_000:
            ts /= 1000.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        return dt, dt.isoformat()

    expiry_text = str(raw_expiry).strip()
    if not expiry_text:
        return None, ""

    if expiry_text.isdigit():
        ts = float(expiry_text)
        if ts > 10_000_000_000:
            ts /= 1000.0
        dt = datetime.fromtimestamp(ts, tz=timezone.utc).date()
        return dt, dt.isoformat()

    try:
        dt = datetime.strptime(expiry_text[:10], "%Y-%m-%d").date()
        return dt, dt.isoformat()
    except ValueError:
        return None, ""


def main():
    print("\n" + "=" * 60)
    print("SILVERMIC Instrument Key Finder")
    print(f"Looking for nearest expiry >= {TODAY}")
    print("=" * 60)

    for source in SOURCES:
        print(f"\n[Attempt] {source['label']}")
        instruments = download_instruments(source)
        if not instruments:
            continue

        print(f"  Instruments loaded: {len(instruments)}")
        matches = find_silvermic(instruments)

        if not matches:
            print("  No SILVERMIC FUT found in this source — trying next")
            continue

        print(f"\n  All active SILVERMIC contracts found:")
        for m in matches:
            marker = " ← FRONT MONTH" if m == matches[0] else ""
            print(f"    {m['symbol']}  expiry={m['expiry']}  key={m['key']}{marker}")

        front = matches[0]
        print("\n" + "=" * 60)
        print(f"Add these 3 lines to {DEFAULT_ENV_FILE}:")
        print("=" * 60)
        print(f"UPSTOX_SILVERMIC_KEY={front['key']}")
        print(f"UPSTOX_SILVERMIC_SYMBOL={front['symbol']}")
        print(f"UPSTOX_SILVERMIC_EXPIRY={front['expiry']}")
        print("=" * 60)
        print("\nThen run:  python main.py --dry-run")
        return

    print("\n❌ All sources failed. Manual fallback:")
    print("   Run this curl in your terminal to see what Upstox returns:")
    print(f"   curl -s 'https://api.upstox.com/v2/instruments?exchange=MCX_FO' \\")
    print(f"     -H 'Authorization: Bearer {ACCESS_TOKEN[:20]}...' | python3 -m json.tool | grep -A5 SILVERMIC")
    sys.exit(1)


if __name__ == "__main__":
    main()
