#!/usr/bin/env python3
"""Crypto options trade journal -> a dedicated Notion database.

Kept separate from the equity/commodity `Trading Journal` on purpose: crypto
options need fields that make no sense there (settlement vs traded exit, USD
commissions, per-leg contract values, extrinsic-at-entry) and mixing them
bloats the main journal.

Source of truth is the Delta project's reconciled campaign JSON under
`~/Projects/Delta/outputs/live/journals/<date>/<campaign-key>.json`, falling
back to the raw event stream for sessions that were never reconciled.

Usage
-----
    PY=~/balas-product-os/Tools/.venv/bin/python

    # one-time, after sharing a Notion page with the integration
    $PY Tools/crypto_journal.py --create-db --parent-page <PAGE_ID>

    # journal one session
    $PY Tools/crypto_journal.py --date 2026-08-01

    # backfill every session found in the Delta event logs
    $PY Tools/crypto_journal.py --backfill

    # preview without writing
    $PY Tools/crypto_journal.py --date 2026-08-01 --dry-run
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DELTA_ROOT = Path.home() / "Projects" / "Delta"
JOURNAL_DIR = DELTA_ROOT / "outputs" / "live" / "journals"
EVENTS_DIR = DELTA_ROOT / "outputs" / "live"
NOTION_VERSION = "2022-06-28"
CONTRACT_VALUE = 0.001  # BTC per contract on Delta India

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Schema
# --------------------------------------------------------------------------
def schema() -> dict[str, Any]:
    """Property definitions for the crypto journal database.

    Net P&L is a formula so it can never drift from Gross - Commission, the
    same guarantee the equity journal relies on.
    """
    sel = lambda *names: {"select": {"options": [{"name": n} for n in names]}}
    return {
        "Trade Label": {"title": {}},
        "Campaign Key": {"rich_text": {}},
        "Trade Date": {"date": {}},
        "Exchange": sel("Delta Exchange India", "Deribit", "Binance", "Other"),
        "Asset": sel("BTC", "ETH", "SOL", "Other"),
        "Strategy": sel(
            "0DTE Short Straddle", "0DTE Short Call", "0DTE Short Put",
            "0DTE Short Strangle", "Single-Leg Retained", "Directional Long Call",
            "Directional Long Put", "Calendar", "Vertical Spread",
            "Research / Paper", "Other",
        ),
        "Structure": sel("Straddle", "Strangle", "Single Leg Call",
                         "Single Leg Put", "Spread", "Other"),
        "Direction": sel("Short Vol", "Long Vol", "Directional Long", "Directional Short"),
        "Expiry": {"date": {}},
        "Strike": {"number": {}},
        "Contracts per Leg": {"number": {}},
        "Contract Value": {"number": {"format": "number"}},
        "Entry Time IST": {"rich_text": {}},
        "Exit Time IST": {"rich_text": {}},
        "Entry Price": {"number": {}},
        "Exit Price": {"number": {}},
        "Exit Mechanism": sel("Time Exit", "Stop Loss", "Settlement",
                              "Manual Close", "Aborted Entry", "Emergency Flatten"),
        "Spot at Entry": {"number": {}},
        "Combined Credit": {"number": {}},
        "Intrinsic at Entry": {"number": {}},
        "Extrinsic at Entry": {"number": {}},
        "Stop Level": {"number": {}},
        "Stop Hit": {"checkbox": {}},
        "Gross PnL USD": {"number": {"format": "dollar"}},
        "Commission USD": {"number": {"format": "dollar"}},
        # Blank, not zero, when either input is missing. A Net that silently
        # equals Gross because commission is unknown reads as a final number
        # and corrupts every downstream fee-drag and expectancy stat.
        "Net PnL USD": {"formula": {"expression":
            'if(or(empty(prop("Commission USD")), empty(prop("Gross PnL USD"))), '
            'toNumber(""), prop("Gross PnL USD") - prop("Commission USD"))'}},
        "Outcome": sel("Win", "Loss", "Breakeven", "Open", "No Trade"),
        "Setup Quality": sel("A+", "A", "B", "C"),
        "Execution": sel("Excellent", "Good", "Average", "Poor"),
        "Automated": {"checkbox": {}},
        "Manual Intervention": {"checkbox": {}},
        "Followed Plan": {"checkbox": {}},
        "Emotions": {"multi_select": {"options": [
            {"name": n} for n in (
                "Calm", "Confident", "Patient", "Disciplined", "Cautious",
                "Impatient", "Anxious", "FOMO", "Greed", "Revenge", "Recovery",
                "Reactive", "Distracted", "Protective", "Aggressive",
                "Hands-off", "Corrective",
            )]}},
        "Pre-trade Notes": {"rich_text": {}},
        "Post-trade Review": {"rich_text": {}},
        "Journal Key": {"rich_text": {}},
    }


class Notion:
    def __init__(self, key: str):
        self.h = {"Authorization": f"Bearer {key}",
                  "Notion-Version": NOTION_VERSION,
                  "Content-Type": "application/json"}

    def create_db(self, parent_page: str) -> dict:
        r = requests.post("https://api.notion.com/v1/databases", headers=self.h, json={
            "parent": {"type": "page_id", "page_id": parent_page},
            # Must be a real emoji; Notion rejects currency symbols such as U+20BF.
            "icon": {"type": "emoji", "emoji": "🪙"},
            "title": [{"type": "text", "text": {"content": "Crypto Options Journal"}}],
            "description": [{"type": "text", "text": {"content":
                "Crypto options campaigns. Separate from the equity/commodity "
                "Trading Journal so neither gets bloated."}}],
            "properties": schema(),
        })
        r.raise_for_status()
        return r.json()

    def find(self, db: str, journal_key: str) -> str | None:
        r = requests.post(f"https://api.notion.com/v1/databases/{db}/query", headers=self.h,
                          json={"filter": {"property": "Journal Key",
                                           "rich_text": {"equals": journal_key}}})
        r.raise_for_status()
        res = r.json().get("results", [])
        return res[0]["id"] if res else None

    def upsert(self, db: str, props: dict, journal_key: str) -> tuple[str, bool]:
        existing = self.find(db, journal_key)
        if existing:
            r = requests.patch(f"https://api.notion.com/v1/pages/{existing}",
                               headers=self.h, json={"properties": props})
            r.raise_for_status()
            return existing, False
        r = requests.post("https://api.notion.com/v1/pages", headers=self.h,
                          json={"parent": {"database_id": db}, "properties": props})
        r.raise_for_status()
        return r.json()["id"], True


# --------------------------------------------------------------------------
# Source data
# --------------------------------------------------------------------------
def load_campaign(day: str) -> dict | None:
    """Prefer the reconciled campaign JSON; fall back to the raw event stream.

    The reconciled schema drifted over time (early files use `date` rather than
    `trade_date`, and `underlying` rather than `asset`), so normalise here
    instead of letting each older file break the mapping.
    """
    folder = JOURNAL_DIR / day
    if folder.is_dir():
        for f in sorted(folder.glob("*.json")):
            c = json.loads(f.read_text())
            c["trade_date"] = iso_date(c.get("trade_date") or c.get("date"), day)
            c.setdefault("asset", c.get("underlying") or "BTC")
            return c
    return reconstruct_from_events(day)


def reconstruct_from_events(day: str) -> dict | None:
    """Best-effort campaign for sessions that were never reconciled by hand."""
    f = EVENTS_DIR / f"events-{day.replace('-', '')}.jsonl"
    if not f.exists():
        return None
    ev = [json.loads(l) for l in f.read_text().splitlines() if l.strip()]
    g = lambda n: [e for e in ev if e.get("event") == n]
    ss, fill, ex, closed = g("session_start"), g("entry_filled"), g("exit_order"), g("closed")
    if not ss:
        return None
    size = ss[0].get("size") or 0
    strike = float(ss[0]["call"].split("-")[2]) if ss and ss[0].get("call") else None
    pre = g("margin_preflight")
    spot = None
    if pre and size:
        spot = float(pre[0]["base_margin"]) / (size * 0.00001)
    credit = float(fill[0]["combined_credit"]) if fill else None
    debit = sum(float(x["fill_price"]) for x in ex) if ex else None
    gross = (credit - debit) * size * CONTRACT_VALUE if (credit and debit is not None) else None
    reason = closed[0].get("reason") if closed else None
    return {
        "campaign_key": f"DELTA-PROD-{day.replace('-','')}-BTC-1700-0DTE",
        "trade_date": day, "asset": "BTC", "strike": strike,
        "expiry": day, "requested_qty_per_leg": size,
        "structure": "STRADDLE",
        "_reconstructed": True,
        "entry": {"combined_credit": credit,
                  "intent_time_ist": ss[0].get("time"),
                  "call": {"average_fill_price": None}},
        "exit": {"reason": reason},
        "pnl_usd": {"day_total": {"gross": gross, "commission": None, "net": None}},
        "market_context": {"spot_at_preflight": spot},
        "monitoring": {"stop_triggered": bool(reason and "stop" in str(reason))},
    }


def hhmm(ts: str | None) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromisoformat(ts).strftime("%H:%M:%S")
    except Exception:
        return str(ts)[11:19]


def iso_date(v, fallback: str | None = None) -> str | None:
    """Normalise a date to ISO. Journals mix ISO and Delta's DD-MM-YYYY."""
    if not v:
        return fallback
    s = str(v).strip()[:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return fallback


def num(v):
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def build_props(c: dict) -> tuple[dict, str, str]:
    """Map a reconciled campaign onto Notion properties."""
    day = c["trade_date"]
    key = c.get("campaign_key") or f"DELTA-PROD-{day.replace('-','')}-BTC"
    structure = (c.get("structure") or "STRADDLE").upper()
    single = "SINGLE_LEG" in structure

    # P&L key names drifted across reconciled journals: newer files nest under
    # day_total with gross/commission/net, older ones use campaign_* at the top
    # level and split commission into entry_/exit_. Accept every variant - a row
    # written with a blank Gross or Commission is worse than no row at all.
    pnl = c.get("pnl_usd", {}) or {}
    total = pnl.get("day_total", pnl)
    gross = num(total.get("gross"))
    if gross is None:
        gross = num(pnl.get("campaign_gross"))
    comm = num(total.get("commission"))
    if comm is None:
        comm = num(pnl.get("campaign_commission")) or num(pnl.get("total_commission"))
    if comm is None:
        entry_c, exit_c = num(pnl.get("entry_commission")), num(pnl.get("exit_commission"))
        if entry_c is not None or exit_c is not None:
            comm = (entry_c or 0) + (exit_c or 0)
    if comm is None:
        leg_c = [num(pnl.get(f"{leg}_commission")) for leg in ("call", "put")]
        if any(x is not None for x in leg_c):
            comm = sum(x for x in leg_c if x is not None)
    # Provisional commissions are NOT promoted into the Commission column - a
    # provisional number silently feeding the Net PnL formula is worse than a
    # visible gap. Record it in the review text instead.
    provisional = num(pnl.get("campaign_commission_provisional"))
    comm_caveat = ""
    if comm is None and provisional is not None:
        comm_caveat = (f"Commission left blank deliberately: the reconciled journal reports "
                       f"${provisional} as PROVISIONAL, not authenticated. Net PnL is "
                       f"incomplete until real fills are reconciled.")
    elif comm is None:
        comm_caveat = ("Commission unavailable: no reconciled journal for this session and "
                       "the Delta fills endpoint only returns the last few days. "
                       "Net PnL is incomplete.")
    net = (gross - comm) if (gross is not None and comm is not None) else \
        num(total.get("net")) or num(pnl.get("campaign_net"))

    mc = c.get("market_context", {}) or {}
    entry, ext = c.get("entry", {}) or {}, c.get("exit", {}) or {}
    manual = c.get("manual_reentry") or {}

    # For a retained single leg the live entry is the manual re-entry when present.
    if manual:
        entry_px, entry_t = num(manual.get("average_fill_price")), manual.get("fill_time_ist")
    elif single:
        entry_px = num((entry.get("call") or {}).get("average_fill_price"))
        entry_t = entry.get("intent_time_ist")
    else:
        entry_px, entry_t = num(entry.get("combined_credit")), entry.get("intent_time_ist")

    exit_call = ext.get("call") or {}
    exit_px = num(exit_call.get("settlement_price") or exit_call.get("average_fill_price")
                  or ext.get("combined_debit"))
    reason = str(ext.get("reason") or "")
    mechanism = ("Settlement" if "settle" in reason else
                 "Stop Loss" if "stop" in reason else
                 "Time Exit" if "time" in reason else
                 "Emergency Flatten" if "emergency" in reason or "flatten" in reason else
                 "Aborted Entry" if "unfilled" in reason else "Manual Close")

    label = (f"BTC {'Short Call' if single else '0DTE Straddle'} "
             f"{c.get('strike') or ''} - {datetime.fromisoformat(day):%b %d}").replace("  ", " ")
    strategy = ("Single-Leg Retained" if single else "0DTE Short Straddle")
    # A session that never filled is "No Trade", not "Open" - it is closed
    # business with no position, and leaving it Open skews any open-risk view.
    if gross is None and net is None:
        outcome = "No Trade"
    elif net is not None:
        outcome = "Win" if net > 0 else "Loss" if net < 0 else "Breakeven"
    else:
        outcome = "Win" if gross > 0 else "Loss" if gross < 0 else "Breakeven"

    props: dict[str, Any] = {
        "Trade Label": {"title": [{"text": {"content": label}}]},
        "Campaign Key": {"rich_text": [{"text": {"content": key}}]},
        "Trade Date": {"date": {"start": day}},
        "Exchange": {"select": {"name": "Delta Exchange India"}},
        "Asset": {"select": {"name": c.get("asset") or "BTC"}},
        "Strategy": {"select": {"name": strategy}},
        "Structure": {"select": {"name": "Single Leg Call" if single else "Straddle"}},
        "Direction": {"select": {"name": "Short Vol"}},
        "Expiry": {"date": {"start": iso_date(c.get("expiry"), day)}},
        "Strike": {"number": num(c.get("strike"))},
        "Contracts per Leg": {"number": num(c.get("requested_qty_per_leg"))},
        "Contract Value": {"number": CONTRACT_VALUE},
        "Entry Time IST": {"rich_text": [{"text": {"content": hhmm(entry_t)}}]},
        "Exit Time IST": {"rich_text": [{"text": {"content": hhmm(exit_call.get("fill_time_ist"))}}]},
        "Entry Price": {"number": entry_px},
        "Exit Price": {"number": exit_px},
        "Exit Mechanism": {"select": {"name": mechanism}},
        "Spot at Entry": {"number": num(mc.get("spot_at_preflight"))},
        "Combined Credit": {"number": num(mc.get("combined_bid_credit") or entry.get("combined_credit"))},
        "Intrinsic at Entry": {"number": num(mc.get("intrinsic"))},
        "Extrinsic at Entry": {"number": num(mc.get("extrinsic"))},
        "Stop Level": {"number": num((manual.get("stop_order") or {}).get("stop_price")
                                     or entry.get("combined_stop_level"))},
        "Stop Hit": {"checkbox": bool((c.get("monitoring") or {}).get("stop_triggered"))},
        "Gross PnL USD": {"number": gross},
        "Commission USD": {"number": comm},
        "Outcome": {"select": {"name": outcome}},
        "Automated": {"checkbox": bool((c.get("review") or {}).get("fully_automated"))},
        "Manual Intervention": {"checkbox": bool((c.get("review") or {}).get("manual_intervention"))},
        "Followed Plan": {"checkbox": bool((c.get("review") or {}).get("followed_trade_plan"))},
        "Journal Key": {"rich_text": [{"text": {"content": key}}]},
    }

    review = c.get("review") or {}
    for field, prop in (("setup_quality", "Setup Quality"), ("execution_quality", "Execution")):
        v = review.get(field)
        if v:
            v = v.split("(")[0].strip().rstrip("/").strip()
            allowed = {"Setup Quality": {"A+", "A", "B", "C"},
                       "Execution": {"Excellent", "Good", "Average", "Poor"}}[prop]
            if v in allowed:
                props[prop] = {"select": {"name": v}}
    # `emotions` is free text in newer journals and a list in some older ones.
    emo = review.get("emotions")
    if isinstance(emo, list):
        emo = " ".join(str(x) for x in emo)
    if emo:
        low = str(emo).lower()
        tag = ("Corrective" if "correct" in low else
               "Hands-off" if ("hands-off" in low or "no manual" in low) else None)
        if tag:
            props["Emotions"] = {"multi_select": [{"name": tag}]}
    pre_note = review.get("thesis") or ""
    post = " ".join(x for x in (review.get("strategy_outcome"), review.get("learning"),
                                review.get("next_rule")) if x)
    if c.get("_reconstructed"):
        pre_note = (pre_note + " [Reconstructed from the raw event stream; this session "
                    "has no reconciled campaign journal.]").strip()
    if comm_caveat:
        post = (post + "  DATA QUALITY: " + comm_caveat).strip()
    if pre_note:
        props["Pre-trade Notes"] = {"rich_text": [{"text": {"content": pre_note[:1900]}}]}
    if post:
        props["Post-trade Review"] = {"rich_text": [{"text": {"content": post[:1900]}}]}
    return props, key, label


def reconcile_from_fills(window_lo=(16, 55), window_hi=(17, 35)) -> dict[str, dict]:
    """Authenticated gross + commission per campaign, straight from Delta fills.

    Fills are the source of truth. Two things this must get right:

    1. Group by IST date AND contract root (strike-expiry). Several days carry
       trades on more than one contract, and a date-level sum silently merges
       them.
    2. Restrict to the 17:00 campaign window. 24 July has an unrelated 08:10
       trade on the same contract that swings the day by roughly six dollars.
    """
    sys.path.insert(0, str(DELTA_ROOT))
    from delta_live.config import Settings as DeltaSettings
    from delta_live.client import DeltaRESTClient

    from datetime import time as dtime, timedelta, timezone
    from collections import defaultdict

    ist = timezone(timedelta(hours=5, minutes=30))
    lo, hi = dtime(*window_lo), dtime(*window_hi)
    client = DeltaRESTClient(DeltaSettings.load(DELTA_ROOT / ".env"))
    fills = client.fills(page_size=500)

    agg: dict[tuple[str, str], dict] = defaultdict(
        lambda: {"gross": 0.0, "comm": 0.0, "n": 0, "excluded": 0})
    for f in fills:
        sym = str(f.get("product_symbol") or "")
        if not sym.startswith(("C-BTC-", "P-BTC-", "C-ETH-", "P-ETH-")):
            continue
        t = datetime.fromisoformat(str(f["created_at"]).replace("Z", "+00:00")).astimezone(ist)
        root = sym.split("-", 2)[2]
        key = (t.date().isoformat(), root)
        if not (lo <= t.time() <= hi):
            agg[key]["excluded"] += 1
            continue
        q, px = float(f["size"]), float(f["price"])
        agg[key]["gross"] += q * px * CONTRACT_VALUE * (1 if f["side"] == "sell" else -1)
        agg[key]["comm"] += float(f.get("commission") or 0)
        agg[key]["n"] += 1

    out: dict[str, dict] = {}
    for (day, root), v in agg.items():
        if v["n"] < 2:
            continue
        # Keep the campaign with the most fills when a day has several.
        if day not in out or v["n"] > out[day]["fills"]:
            out[day] = {"contract": root, "gross": round(v["gross"], 6),
                        "commission": round(v["comm"], 8),
                        "net": round(v["gross"] - v["comm"], 6),
                        "fills": v["n"], "excluded_out_of_window": v["excluded"]}
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Journal crypto options trades to Notion")
    p.add_argument("--create-db", action="store_true")
    p.add_argument("--parent-page", help="Notion page ID to create the database under")
    p.add_argument("--date", help="YYYY-MM-DD")
    p.add_argument("--backfill", action="store_true")
    p.add_argument("--reconcile-fees", action="store_true",
                   help="Overwrite Gross/Commission from authenticated Delta fills")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    args = p.parse_args()

    load_dotenv(args.env_file)
    key = os.getenv("NOTION_API_KEY")
    if not key:
        logger.error("NOTION_API_KEY not found in %s", args.env_file)
        return 1
    api = Notion(key)

    if args.create_db:
        if not args.parent_page:
            logger.error("--create-db needs --parent-page <PAGE_ID>")
            return 1
        db = api.create_db(args.parent_page)
        print(f"\nCreated database: {db['id']}\nURL: {db.get('url')}\n")
        print("Add this line to ~/balas-product-os/.env:")
        print(f"NOTION_CRYPTO_JOURNAL_DB={db['id']}\n")
        return 0

    db = os.getenv("NOTION_CRYPTO_JOURNAL_DB")
    if not db:
        logger.error("NOTION_CRYPTO_JOURNAL_DB not set. Run --create-db first.")
        return 1

    if args.reconcile_fees:
        recon = reconcile_from_fills()
        rows = requests.post(f"https://api.notion.com/v1/databases/{db}/query",
                             headers=api.h, json={"page_size": 100}).json()["results"]
        touched = 0
        for row in rows:
            props = row["properties"]
            day = (props["Trade Date"]["date"] or {}).get("start")
            r = recon.get(day)
            if not r:
                logger.warning("%s: no authenticated fills found, leaving as-is", day)
                continue
            old_g = props["Gross PnL USD"]["number"]
            old_c = props["Commission USD"]["number"]
            note = ("Gross and commission reconciled from authenticated Delta fills "
                    f"({r['fills']} fills on {r['contract']}"
                    + (f", {r['excluded_out_of_window']} out-of-window fills excluded"
                       if r["excluded_out_of_window"] else "") + ").")
            review = "".join(x["plain_text"] for x in props["Post-trade Review"]["rich_text"])
            review = review.split("  DATA QUALITY:")[0].strip()
            upd = {
                "Gross PnL USD": {"number": r["gross"]},
                "Commission USD": {"number": r["commission"]},
                "Post-trade Review": {"rich_text": [{"text": {"content":
                    (review + "  RECONCILED: " + note)[:1990]}}]},
            }
            if r["gross"] is not None:
                upd["Outcome"] = {"select": {"name":
                    "Win" if r["net"] > 0 else "Loss" if r["net"] < 0 else "Breakeven"}}
            if args.dry_run:
                logger.info("[DRY] %s gross %s -> %s | comm %s -> %s",
                            day, old_g, r["gross"], old_c, r["commission"])
                continue
            resp = requests.patch(f"https://api.notion.com/v1/pages/{row['id']}",
                                  headers=api.h, json={"properties": upd})
            resp.raise_for_status()
            logger.info("Reconciled %s: gross %s comm %s (net %s)",
                        day, r["gross"], r["commission"], r["net"])
            touched += 1
        logger.info("Done. reconciled=%d", touched)
        return 0

    if args.backfill:
        days = sorted({f.stem.replace("events-", "") for f in EVENTS_DIR.glob("events-*.jsonl")})
        days = [f"{d[:4]}-{d[4:6]}-{d[6:]}" for d in days]
    elif args.date:
        days = [args.date]
    else:
        days = [date.today().isoformat()]

    created = updated = skipped = 0
    for day in days:
        c = load_campaign(day)
        if not c:
            logger.info("%s: no campaign data, skipping", day)
            skipped += 1
            continue
        props, jkey, label = build_props(c)
        if args.dry_run:
            g = props["Gross PnL USD"]["number"]
            cm = props["Commission USD"]["number"]
            logger.info("[DRY] %s | %s | gross=%s comm=%s | %s",
                        day, label, g, cm, props["Exit Mechanism"]["select"]["name"])
            continue
        _, was_new = api.upsert(db, props, jkey)
        logger.info("%s %s", "Created:" if was_new else "Updated:", label)
        created += was_new
        updated += not was_new

    logger.info("Done. created=%d updated=%d skipped=%d", created, updated, skipped)
    return 0


if __name__ == "__main__":
    sys.exit(main())
