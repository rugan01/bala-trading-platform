#!/usr/bin/env python3
"""
Nifty Volatility Surface Monitor
==================================
Four-agent pipeline that runs daily at market open, builds a live
implied-volatility surface across all available tenors, and infers
what the market is pricing — including geopolitical tail risk.

Agents:
  1  Data Collection  — fetch all expiries + option chains from Upstox
  2  Surface Builder  — IV inversion (Black-Scholes, Newton-Raphson) per strike/tenor
  3  Geopolitical     — term structure, skew, OI hedging signals → plain-English inference
  4  Persistence      — charts, daily inference log, regime-change detection vs yesterday

Outputs:
  Projects/trading-system/vol-surface/charts/YYYY-MM-DD/
  Projects/trading-system/vol-surface/inference-log/YYYY-MM/YYYY-MM-DD.md
"""

import re, sys, math, json
from typing import Optional
import numpy as np
import pandas as pd
import requests
from datetime import date, datetime, timedelta
from pathlib import Path
from scipy.optimize import brentq
from scipy.stats import norm

# ── Paths ─────────────────────────────────────────────────────────────────────
# Repo-relative, matching the other briefing apps. Was hardcoded to the
# workspace checkout, which read a different .env from the rest of the brief.
BASE_DIR    = Path(__file__).resolve().parents[2]
OUT_ROOT    = BASE_DIR / "data" / "reports" / "vol-surface"
TODAY_STR   = date.today().isoformat()
CHART_DIR   = OUT_ROOT / "charts" / TODAY_STR
LOG_DIR     = OUT_ROOT / "inference-log" / TODAY_STR[:7]
LOG_FILE    = LOG_DIR / f"{TODAY_STR}.md"
LAST_LOG    = None  # resolved after log dir scanned

for d in [CHART_DIR, LOG_DIR]:
    d.mkdir(parents=True, exist_ok=True)

# ── Upstox ────────────────────────────────────────────────────────────────────
BASE_URL = "https://api.upstox.com/v2"

def load_token() -> str:
    """Read the Upstox token from the repo .env.

    Accepts single-quoted, double-quoted or bare values - the token refresher
    and hand edits do not agree on quoting, and a quote mismatch previously
    surfaced as an opaque 401 rather than a missing-token error.
    """
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        sys.exit(f"No .env at {env_path}")
    m = re.search(r"""UPSTOX_BALA_ACCESS_TOKEN=['"]?([^'"\s]+)['"]?""", env_path.read_text())
    if not m:
        sys.exit(f"UPSTOX_BALA_ACCESS_TOKEN not found in {env_path}")
    return m.group(1)

TOKEN   = load_token()
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Accept": "application/json"}

def upstox_get(endpoint: str, params: dict = None) -> dict:
    r = requests.get(f"{BASE_URL}/{endpoint}", params=params, headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()

# ─────────────────────────────────────────────────────────────────────────────
# BLACK-SCHOLES ENGINE
# ─────────────────────────────────────────────────────────────────────────────
RISK_FREE = 0.065  # RBI repo rate proxy

def bs_price(S, K, T, r, sigma, opt="C") -> float:
    """Standard BSM call/put price."""
    if T <= 0 or sigma <= 0:
        return max(0, S - K) if opt == "C" else max(0, K - S)
    d1 = (math.log(S / K) + (r + 0.5 * sigma**2) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if opt == "C":
        return S * norm.cdf(d1) - K * math.exp(-r * T) * norm.cdf(d2)
    else:
        return K * math.exp(-r * T) * norm.cdf(-d2) - S * norm.cdf(-d1)

def implied_vol(market_price: float, S: float, K: float, T: float,
                r: float = RISK_FREE, opt: str = "C") -> Optional[float]:
    """
    Brent's method IV inversion.
    Returns None if price is below intrinsic or BS inversion fails.
    """
    if T <= 0 or market_price <= 0:
        return None
    intrinsic = max(0, S - K) if opt == "C" else max(0, K - S)
    if market_price <= intrinsic + 0.01:
        return None
    try:
        def objective(sigma):
            return bs_price(S, K, T, r, sigma, opt) - market_price
        iv = brentq(objective, 1e-4, 5.0, xtol=1e-6, maxiter=200)
        return iv if 0.01 < iv < 4.0 else None
    except (ValueError, RuntimeError):
        return None

# ─────────────────────────────────────────────────────────────────────────────
# AGENT 1 — DATA COLLECTION
# ─────────────────────────────────────────────────────────────────────────────
def agent1_collect() -> tuple[float, list[str], dict]:
    print("\n" + "="*65)
    print("AGENT 1 — DATA COLLECTION")
    print("="*65)

    # Spot
    spot_data = upstox_get("market-quote/ltp",
                            {"instrument_key": "NSE_INDEX|Nifty 50"})
    spot = spot_data["data"]["NSE_INDEX:Nifty 50"]["last_price"]
    print(f"Nifty Spot: {spot:,.2f}")

    # All available expiries
    contracts = upstox_get("option/contract",
                            {"instrument_key": "NSE_INDEX|Nifty 50"})
    all_expiries_raw = sorted(set(
        c["expiry"] for c in contracts.get("data", [])
        if c.get("expiry")
    ))
    # Keep next 8 expiries max (covers ~2 months of weekly + monthlies)
    today = date.today()
    expiries = [
        e for e in all_expiries_raw
        if date.fromisoformat(e) > today
    ][:8]
    print(f"Expiries to analyse ({len(expiries)}): {expiries}")

    # Fetch chains
    chains: dict[str, list] = {}
    for exp in expiries:
        try:
            r = upstox_get("option/chain",
                           {"instrument_key": "NSE_INDEX|Nifty 50",
                            "expiry_date": exp})
            rows = r.get("data", [])
            chains[exp] = rows
            dte = (date.fromisoformat(exp) - today).days
            print(f"  {exp} ({dte:2d}d): {len(rows)} strikes loaded")
        except Exception as ex:
            print(f"  {exp}: FAILED — {ex}")

    return spot, expiries, chains

# ─────────────────────────────────────────────────────────────────────────────
# AGENT 2 — VOL SURFACE BUILDER
# ─────────────────────────────────────────────────────────────────────────────
def agent2_build_surface(spot: float, expiries: list[str],
                          chains: dict) -> pd.DataFrame:
    print("\n" + "="*65)
    print("AGENT 2 — VOL SURFACE CONSTRUCTION")
    print("="*65)

    today = date.today()
    records = []

    for exp in expiries:
        dte = (date.fromisoformat(exp) - today).days
        T   = dte / 365.0
        rows = chains.get(exp, [])
        if not rows:
            continue

        exp_ce_oi = sum(r.get("call_options", {}).get("market_data", {}).get("oi", 0) for r in rows)
        exp_pe_oi = sum(r.get("put_options",  {}).get("market_data", {}).get("oi", 0) for r in rows)
        pcr = exp_pe_oi / exp_ce_oi if exp_ce_oi > 0 else 1.0

        for row in rows:
            strike  = row.get("strike_price", 0)
            moneyness = strike / spot
            # Filter to ±18% moneyness — outside this, prices are too thin
            if not (0.82 <= moneyness <= 1.18):
                continue

            ce = row.get("call_options", {}).get("market_data", {})
            pe = row.get("put_options",  {}).get("market_data", {})

            ce_ltp = ce.get("ltp", 0) or 0
            pe_ltp = pe.get("ltp", 0) or 0
            ce_oi  = ce.get("oi", 0) or 0
            pe_oi  = pe.get("oi", 0) or 0

            # Minimum price filter — very cheap OTM options have noisy IV
            ce_iv = implied_vol(ce_ltp, spot, strike, T, opt="C") if ce_ltp >= 3 else None
            pe_iv = implied_vol(pe_ltp, spot, strike, T, opt="P") if pe_ltp >= 3 else None

            records.append({
                "expiry":    exp,
                "dte":       dte,
                "T":         T,
                "strike":    strike,
                "moneyness": moneyness,
                "ce_ltp":    ce_ltp,
                "pe_ltp":    pe_ltp,
                "ce_oi":     ce_oi,
                "pe_oi":     pe_oi,
                "ce_iv":     ce_iv,
                "pe_iv":     pe_iv,
                "pcr":       pcr,
                # Use call IV for OTM calls, put IV for OTM puts, average at ATM
                "iv":        (ce_iv if moneyness >= 1.0 else pe_iv),
            })

    df = pd.DataFrame(records)

    # Summary by expiry
    print(f"\n{'Expiry':<12} {'DTE':>4} {'ATM IV':>7} {'PCR':>5} {'Strikes w/IV':>12}")
    print("-" * 50)
    for exp in expiries:
        sub = df[df.expiry == exp]
        atm = sub.iloc[(sub.moneyness - 1.0).abs().argsort()[:1]]
        atm_iv = (atm.ce_iv.values[0] if atm.ce_iv.values[0] else
                  atm.pe_iv.values[0] if len(atm) > 0 else None)
        valid  = sub.iv.notna().sum()
        pcr_v  = sub.pcr.iloc[0] if len(sub) > 0 else 0
        dte_v  = sub.dte.iloc[0] if len(sub) > 0 else 0
        iv_str = f"{atm_iv*100:.1f}%" if atm_iv else "  N/A"
        print(f"{exp:<12} {dte_v:>4} {iv_str:>7} {pcr_v:>5.2f} {valid:>12}")

    print(f"\nTotal records with valid IV: {df.iv.notna().sum()} / {len(df)}")
    return df

# ─────────────────────────────────────────────────────────────────────────────
# AGENT 3 — GEOPOLITICAL INFERENCE
# ─────────────────────────────────────────────────────────────────────────────
def agent3_infer(spot: float, df: pd.DataFrame,
                 geopolitical_context: str = "Iran war risk") -> dict:
    print("\n" + "="*65)
    print(f"AGENT 3 — GEOPOLITICAL INFERENCE [{geopolitical_context}]")
    print("="*65)

    today = date.today()
    insights = {}

    # ── Term Structure ─────────────────────────────────────────────────────
    term_struct = []
    for exp in df.expiry.unique():
        sub = df[df.expiry == exp]
        atm_sub = sub.iloc[(sub.moneyness - 1.0).abs().argsort()[:3]]
        # Use the OTM-side IV column, not ce_iv. implied_vol() inverts against
        # SPOT, but Indian index options price off the futures, which carries a
        # premium. An ITM call therefore looks cheap to a spot-based model and
        # returns a depressed IV. Since the ATM band straddles spot, half those
        # strikes have ITM calls, which dragged the whole term structure down
        # (2026-08-03: 3.1% printed against a true ATM IV near 10%). The `iv`
        # column already applies the correct convention - OTM call above spot,
        # OTM put below - so it is unaffected.
        atm_iv = atm_sub.iv.dropna().mean()
        if not atm_iv or math.isnan(atm_iv):
            atm_iv = atm_sub.ce_iv.dropna().mean()   # fallback, old behaviour
        if atm_iv and not math.isnan(atm_iv):
            dte = sub.dte.iloc[0]
            term_struct.append({"expiry": exp, "dte": dte, "atm_iv": atm_iv})

    term_struct = sorted(term_struct, key=lambda x: x["dte"])
    insights["term_structure"] = term_struct

    # Near-expiry IV rises mechanically as time value collapses, and the smile
    # steepens sharply, so an expiry inside a few days will read as an "inverted
    # term structure" on any ordinary trading day. On 2026-08-03 the 1-DTE leg
    # printed 12.8% against 10.6% far-dated and produced a 35.1/100 geopolitical
    # score on a day the actual news was DE-escalation (Iran strikes cancelled,
    # crude -5.7%). The tell that it was an artifact: the RBI decision fell
    # inside the 8-DTE expiry and outside the 1-DTE one, yet the RBI expiry was
    # the CHEAPEST on the board. Real event risk bids the expiry containing the
    # event. Short-dated expiries are still shown, just excluded from the slope.
    MIN_TERM_STRUCT_DTE = 3
    eligible = [t for t in term_struct if t["dte"] >= MIN_TERM_STRUCT_DTE]
    excluded = [t for t in term_struct if t["dte"] < MIN_TERM_STRUCT_DTE]

    # Term structure slope: positive = normal (future > near), negative = inverted (fear)
    if len(eligible) >= 2:
        near_iv = eligible[0]["atm_iv"]
        far_iv  = eligible[-1]["atm_iv"]
        slope   = far_iv - near_iv  # positive = normal, negative = inverted
        ratio   = near_iv / far_iv if far_iv > 0 else 1.0
        insights["term_slope"]       = slope
        insights["term_ratio"]       = ratio
        insights["near_iv"]          = near_iv
        insights["far_iv"]           = far_iv
        insights["near_dte"]         = eligible[0]["dte"]
        insights["near_expiry"]      = eligible[0]["expiry"]
        insights["far_expiry"]       = eligible[-1]["expiry"]
        insights["ts_inverted"]      = slope < -0.005  # >0.5pp inversion = signal
        insights["ts_excluded_dte"]  = [t["dte"] for t in excluded]

        print(f"\nTerm Structure:")
        for ts in term_struct:
            bar = "█" * int(ts["atm_iv"] * 200)
            tag = "  (excluded from slope: expiry effect)" if ts in excluded else ""
            print(f"  {ts['expiry']} ({ts['dte']:2d}d)  {ts['atm_iv']*100:5.1f}%  {bar}{tag}")
        if excluded:
            print(f"\n  NOTE: {len(excluded)} expiry(ies) under {MIN_TERM_STRUCT_DTE} DTE excluded from the "
                  f"slope. Near-dated IV rises mechanically into expiry and would\n"
                  f"        otherwise register as a false 'near-term fear premium'.")
        print(f"\n  Slope (far–near): {slope*100:+.1f}pp  |  Ratio: {ratio:.2f}x"
              f"  [{eligible[0]['dte']}d vs {eligible[-1]['dte']}d]")
        if insights["ts_inverted"]:
            print("  ⚠  INVERTED TERM STRUCTURE — near-term fear premium active")
            print("     Cross-check: is a scheduled event inside the near expiry and outside")
            print("     the far one? If the event-bearing expiry is NOT the bid one, treat as noise.")
        else:
            print("  ✓  Normal shape — no acute front-month event risk priced")
    else:
        insights["ts_inverted"] = False
        insights["term_slope"]  = 0
        insights["term_ratio"]  = 1.0
        insights["ts_excluded_dte"] = [t["dte"] for t in excluded]
        if term_struct:
            print(f"\nTerm Structure: only {len(eligible)} expiry at/over {MIN_TERM_STRUCT_DTE} DTE "
                  f"- slope not computed, treated as neutral.")

    # ── Skew Analysis (Risk Reversal per tenor) ────────────────────────────
    skew_results = []
    for exp in df.expiry.unique():
        sub  = df[df.expiry == exp].copy().sort_values("moneyness")
        dte  = sub.dte.iloc[0]
        T    = sub.T.iloc[0]

        # 25-delta proxy: ~±0.05 moneyness from ATM
        otm_put  = sub[sub.moneyness.between(0.93, 0.97)].pe_iv.dropna()
        otm_call = sub[sub.moneyness.between(1.03, 1.07)].ce_iv.dropna()
        atm_row  = sub.iloc[(sub.moneyness - 1.0).abs().argsort()[:3]]
        atm_iv   = atm_row.ce_iv.dropna().mean()

        if len(otm_put) > 0 and len(otm_call) > 0 and atm_iv:
            rr = otm_put.mean() - otm_call.mean()  # positive = put skew (downside fear)
            bf = (otm_put.mean() + otm_call.mean()) / 2 - atm_iv  # butterfly = convexity
            skew_results.append({"expiry": exp, "dte": dte, "rr": rr, "bf": bf,
                                  "atm_iv": atm_iv})

    insights["skew"] = skew_results
    if skew_results:
        print(f"\nRisk Reversal (put_IV – call_IV at ~25-delta):")
        print(f"  {'Expiry':<12} {'DTE':>4} {'RR':>7} {'Butterfly':>10} {'Signal'}")
        print("  " + "-"*55)
        for s in sorted(skew_results, key=lambda x: x["dte"]):
            rr_sign = "PUT SKEW (downside fear)" if s["rr"] > 0.005 else \
                      "CALL SKEW (upside chase)" if s["rr"] < -0.005 else "Balanced"
            print(f"  {s['expiry']:<12} {s['dte']:>4} {s['rr']*100:>+6.1f}pp {s['bf']*100:>9.1f}pp  {rr_sign}")

    # ── OI Hedging Signals ─────────────────────────────────────────────────
    # Look at concentration of put OI at low strikes (disaster hedging)
    disaster_put_pct = {}
    for exp in df.expiry.unique():
        sub = df[df.expiry == exp]
        total_pe = sub.pe_oi.sum()
        deep_pe  = sub[sub.moneyness < 0.93].pe_oi.sum()
        if total_pe > 0:
            pct = deep_pe / total_pe * 100
            disaster_put_pct[exp] = pct

    insights["disaster_hedge_pct"] = disaster_put_pct
    if disaster_put_pct:
        print(f"\nDeep OTM Put OI (moneyness <0.93) as % of total put OI:")
        for exp, pct in sorted(disaster_put_pct.items()):
            bar   = "▓" * int(pct / 3)
            flag  = "  ← ELEVATED" if pct > 30 else ""
            print(f"  {exp}: {pct:5.1f}% {bar}{flag}")

    # ── Geopolitical Risk Score (0–100) ────────────────────────────────────
    score = 0
    score_breakdown = {}

    # Component 1: Term structure inversion (max 25 pts)
    ratio = insights.get("term_ratio", 1.0)
    ts_score = min(25, max(0, (ratio - 1.0) * 100))
    score += ts_score
    score_breakdown["term_structure_inversion"] = round(ts_score, 1)

    # Component 2: Near-term put skew (max 25 pts)
    # Same 3-DTE floor as the term structure: the smile steepens sharply into
    # expiry, so a sub-3-DTE risk reversal measures time decay, not positioning.
    near_skews = [s["rr"] for s in skew_results if 3 <= s["dte"] <= 21]
    skew_score = min(25, max(0, np.mean(near_skews) * 200)) if near_skews else 0
    score += skew_score
    score_breakdown["near_term_put_skew"] = round(skew_score, 1)

    # Component 3: Absolute IV level vs baseline 12% normal (max 25 pts)
    near_iv = insights.get("near_iv", 0.12)
    iv_score = min(25, max(0, (near_iv - 0.12) * 250))
    score += iv_score
    score_breakdown["absolute_iv_elevation"] = round(iv_score, 1)

    # Component 4: Disaster hedge concentration (max 25 pts)
    avg_disaster = np.mean(list(disaster_put_pct.values())) if disaster_put_pct else 0
    disaster_score = min(25, max(0, (avg_disaster - 20) * 2))
    score += disaster_score
    score_breakdown["disaster_hedge_concentration"] = round(disaster_score, 1)

    score = round(score, 1)
    insights["geo_risk_score"] = score
    insights["score_breakdown"] = score_breakdown

    # Risk label
    if score < 20:
        risk_label = "LOW — market not pricing geopolitical tail risk"
    elif score < 40:
        risk_label = "MODERATE — some premium in vol but not event-level pricing"
    elif score < 60:
        risk_label = "ELEVATED — options market signals meaningful tail risk being hedged"
    else:
        risk_label = "HIGH — options structure consistent with imminent shock pricing"

    insights["risk_label"] = risk_label
    print(f"\nGeopolitical Risk Score [{geopolitical_context}]: {score:.1f}/100  →  {risk_label}")
    print(f"  Breakdown: {score_breakdown}")

    # ── Plain-English Inference ────────────────────────────────────────────
    inferences = _build_inference_text(
        insights, spot, geopolitical_context
    )
    insights["inference_text"] = inferences
    print(f"\n{'─'*65}")
    print(inferences)

    return insights

def _build_inference_text(ins: dict, spot: float, context: str) -> str:
    ts_inv  = ins.get("ts_inverted", False)
    near_iv = ins.get("near_iv", 0.12)
    far_iv  = ins.get("far_iv", 0.12)
    slope   = ins.get("term_slope", 0)
    score   = ins.get("geo_risk_score", 0)
    skews   = ins.get("skew", [])
    near_skew = next((s for s in sorted(skews, key=lambda x: x["dte"]) if s["dte"] <= 21), None)

    lines = [f"MARKET INFERENCE — {date.today().isoformat()}  [{context}]", ""]

    # Term structure reading
    if ts_inv:
        lines.append(
            f"The term structure is INVERTED. Near-term IV ({near_iv*100:.1f}%) is running "
            f"above far-term IV ({far_iv*100:.1f}%). This is the options market saying: "
            f"something specific is expected to happen soon, not later. A spread of "
            f"{abs(slope)*100:.1f}pp is above the noise threshold."
        )
    else:
        lines.append(
            f"The term structure is NORMAL — near-term IV ({near_iv*100:.1f}%) runs below "
            f"far-term IV ({far_iv*100:.1f}%, slope {slope*100:+.1f}pp). The market is NOT pricing "
            f"an imminent specific event. Volatility expectations rise with time, "
            f"which is the standard regime when nothing acute is being hedged."
        )

    lines.append("")

    # Skew reading
    if near_skew:
        rr = near_skew["rr"]
        if rr > 0.01:
            lines.append(
                f"Put-call skew at the nearest expiry ({near_skew['expiry']}) shows {rr*100:+.1f}pp "
                f"put premium over calls at similar delta. This is active downside hedging — "
                f"institutions are paying up to protect portfolios. When skew is this directional, "
                f"it generally reflects position-level hedging, not speculation."
            )
        elif rr < -0.01:
            lines.append(
                f"The skew at {near_skew['expiry']} is NEGATIVE ({rr*100:+.1f}pp) — calls are "
                f"more expensive than equidistant puts. This is the rarer regime. The market "
                f"has more upside positioning than downside protection at near-term tenors. "
                f"Counter-intuitive given the geopolitical backdrop."
            )
        else:
            lines.append(
                f"Skew at {near_skew['expiry']} is balanced ({rr*100:+.1f}pp). "
                f"No directional tilt in hedging demand."
            )

    lines.append("")

    # Geopolitical conclusion
    lines.append(f"WHAT THE OPTIONS MARKET IS SAYING ABOUT [{context.upper()}]:")
    lines.append("")

    if score < 20:
        lines.append(
            f"Score {score}/100. The options surface is NOT pricing {context} as a near-term "
            f"market-moving event. IV is near historical normal, the term structure slopes "
            f"conventionally, and put skew is not elevated. Either the market has discounted "
            f"the event entirely, or it believes Nifty is sufficiently insulated "
            f"(oil shock thesis: NSE more rate-sensitive than oil-export sensitive)."
        )
        lines.append("")
        lines.append(
            f"What it CANNOT predict: the first-order effect of a supply shock on crude. "
            f"Nifty has historically been impacted with a 2-4 week lag via FII outflows "
            f"and inflation expectations, not from the event day itself. The options surface "
            f"reflects the known — it is not a crystal ball for the unknown."
        )
    elif score < 40:
        lines.append(
            f"Score {score}/100. Moderate risk premium present. The surface shows some "
            f"asymmetry — either in the term structure or in put skew — but not at "
            f"levels consistent with imminent shock pricing. The market is hedging "
            f"without conviction. Straddle prices suggest an expected move of "
            f"±{ins.get('near_iv', 0.12) * math.sqrt(term_struct_dte(ins)/365) * spot:,.0f} pts "
            f"by the next expiry, which is within normal range."
        )
    elif score < 60:
        lines.append(
            f"Score {score}/100. The surface is in elevated-risk regime. Multiple signals "
            f"align: term structure compression, put skew at near-term expiries, and "
            f"above-baseline IV. The options market is hedging a meaningful probability "
            f"of a sharp move. The straddle implies the market expects uncertainty — "
            f"not necessarily a crash, but a wide outcome distribution."
        )
    else:
        lines.append(
            f"Score {score}/100. HIGH RISK regime. The surface structure — inverted term "
            f"structure, elevated put skew, and IV significantly above baseline — is "
            f"consistent with pricing of a near-term shock. In historical analogues "
            f"(COVID March 2020, Feb 2022 invasion), this configuration appeared 5-15 "
            f"trading days before realized volatility spiked."
        )

    lines.append("")
    lines.append(
        f"LIMITATIONS: The options market prices probability distributions, not outcomes. "
        f"A score of 20 does not mean the event won't happen — it means the market "
        f"currently assigns low probability to it moving Nifty materially. The market "
        f"has been wrong about geopolitical events with high frequency. "
        f"Use this as one input, not a forecast."
    )

    return "\n".join(lines)

def term_struct_dte(ins: dict) -> int:
    """DTE that pairs with insights['near_iv'] for the expected-move calc.

    Must track the same expiry the slope used, otherwise a sub-3-DTE leg is
    excluded from the slope but still scales the expected move.
    """
    if ins.get("near_dte"):
        return ins["near_dte"]
    ts = ins.get("term_structure", [])
    return ts[0]["dte"] if ts else 28

# ─────────────────────────────────────────────────────────────────────────────
# CHART GENERATION
# ─────────────────────────────────────────────────────────────────────────────
def generate_charts(spot: float, df: pd.DataFrame, insights: dict):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import cm
        from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
    except ImportError:
        print("matplotlib unavailable — skipping charts")
        return

    plt.rcParams.update({
        "text.color":      "#e5e7eb",
        "axes.labelcolor": "#9ca3af",
        "xtick.color":     "#9ca3af",
        "ytick.color":     "#9ca3af",
        "figure.facecolor": "#0d1117",
        "axes.facecolor":  "#161b22",
        "axes.edgecolor":  "#374151",
        "grid.color":      "#1f2937",
    })

    # ── Chart 1: Vol Surface (3D) ──────────────────────────────────────────
    surf_df = df.dropna(subset=["iv"]).copy()
    if len(surf_df) >= 10:
        fig = plt.figure(figsize=(14, 8))
        ax  = fig.add_subplot(111, projection="3d")
        ax.set_facecolor("#161b22")
        fig.patch.set_facecolor("#0d1117")

        pivot = surf_df.pivot_table(index="moneyness", columns="dte", values="iv", aggfunc="mean")
        X, Y  = np.meshgrid(pivot.columns.values, pivot.index.values)
        Z     = pivot.values

        # Fill small gaps via forward-fill
        Z_filled = pd.DataFrame(Z).interpolate(method="linear", axis=1).values

        mask  = ~np.isnan(Z_filled)
        if mask.sum() > 3:
            surf = ax.plot_surface(X, Y, Z_filled * 100,
                                   cmap=cm.plasma, alpha=0.85,
                                   linewidth=0, antialiased=True)
            fig.colorbar(surf, ax=ax, shrink=0.5, aspect=10,
                         label="Implied Vol (%)", pad=0.1)

        ax.set_xlabel("DTE", labelpad=10)
        ax.set_ylabel("Moneyness (K/S)", labelpad=10)
        ax.set_zlabel("IV (%)", labelpad=10)
        ax.set_title(f"Nifty Vol Surface — {TODAY_STR}\nSpot: {spot:,.0f}",
                     color="white", fontsize=13, pad=20)
        ax.view_init(elev=30, azim=-60)

        plt.tight_layout()
        p = CHART_DIR / "01_vol_surface_3d.png"
        plt.savefig(p, dpi=140, bbox_inches="tight")
        plt.close()
        print(f"  Saved: {p.name}")

    # ── Chart 2: Term Structure (ATM IV by tenor) ──────────────────────────
    ts = insights.get("term_structure", [])
    if ts:
        fig, ax = plt.subplots(figsize=(10, 5))
        dtes  = [t["dte"]    for t in ts]
        ivs   = [t["atm_iv"] * 100 for t in ts]
        expls = [t["expiry"] for t in ts]

        ax.plot(dtes, ivs, "o-", color="#60a5fa", linewidth=2.5,
                markersize=8, markerfacecolor="#1d4ed8")
        for d, v, e in zip(dtes, ivs, expls):
            ax.annotate(f"{e}\n{v:.1f}%", (d, v), textcoords="offset points",
                        xytext=(0, 12), ha="center", fontsize=8, color="#9ca3af")

        # Shade region
        ax.fill_between(dtes, ivs, alpha=0.15, color="#60a5fa")

        ax.set_xlabel("Days to Expiry")
        ax.set_ylabel("ATM Implied Vol (%)")
        ax.set_title(f"Nifty Term Structure — {TODAY_STR}",
                     color="white", fontsize=13)
        ax.grid(True, alpha=0.3)

        # Annotate slope
        slope = insights.get("term_slope", 0)
        shape = "INVERTED ⚠" if insights.get("ts_inverted") else "Normal"
        ax.text(0.98, 0.05,
                f"Shape: {shape}  |  Slope: {slope*100:+.1f}pp",
                transform=ax.transAxes, ha="right", fontsize=9,
                color="#f59e0b" if insights.get("ts_inverted") else "#9ca3af",
                bbox=dict(boxstyle="round", facecolor="#1f2937", edgecolor="#374151"))

        plt.tight_layout()
        p = CHART_DIR / "02_term_structure.png"
        plt.savefig(p, dpi=140, bbox_inches="tight")
        plt.close()
        print(f"  Saved: {p.name}")

    # ── Chart 3: Skew Across Tenors ────────────────────────────────────────
    skews = insights.get("skew", [])
    if skews:
        fig, axes = plt.subplots(1, 2, figsize=(14, 5))

        # Left: Risk reversal by tenor
        ax = axes[0]
        dtes = [s["dte"] for s in skews]
        rrs  = [s["rr"] * 100 for s in skews]
        colors = ["#ef4444" if r > 0 else "#22c55e" for r in rrs]
        ax.bar(dtes, rrs, color=colors, alpha=0.8, width=3)
        ax.axhline(0, color="#6b7280", linewidth=1, linestyle="--")
        ax.set_xlabel("Days to Expiry")
        ax.set_ylabel("Risk Reversal (put IV – call IV, pp)")
        ax.set_title("Put-Call Skew by Tenor\n(+ve = put premium, downside fear)",
                     color="white", fontsize=11)
        ax.grid(True, alpha=0.3, axis="y")

        # Right: Vol smile for nearest expiry
        ax2 = axes[1]
        nearest_exp = sorted(df.expiry.unique(), key=lambda e: (date.fromisoformat(e) - date.today()).days)[0]
        near_df = df[df.expiry == nearest_exp].sort_values("moneyness")
        ce_valid = near_df.dropna(subset=["ce_iv"])
        pe_valid = near_df.dropna(subset=["pe_iv"])

        ax2.plot(ce_valid.moneyness, ce_valid.ce_iv * 100, "o-",
                 color="#22c55e", label="Call IV", linewidth=2, markersize=5)
        ax2.plot(pe_valid.moneyness, pe_valid.pe_iv * 100, "s-",
                 color="#ef4444", label="Put IV",  linewidth=2, markersize=5)
        ax2.axvline(1.0, color="#facc15", linewidth=1.5, linestyle="--", alpha=0.7, label="ATM")
        ax2.set_xlabel("Moneyness (K/S)")
        ax2.set_ylabel("Implied Vol (%)")
        ax2.set_title(f"Vol Smile — {nearest_exp}",
                      color="white", fontsize=11)
        ax2.legend(facecolor="#1f2937", labelcolor="white", fontsize=9)
        ax2.grid(True, alpha=0.3)

        plt.tight_layout()
        p = CHART_DIR / "03_skew_and_smile.png"
        plt.savefig(p, dpi=140, bbox_inches="tight")
        plt.close()
        print(f"  Saved: {p.name}")

    # ── Chart 4: Geopolitical Risk Score History ───────────────────────────
    _plot_risk_score_history(insights.get("geo_risk_score", 0))

def _plot_risk_score_history(today_score: float):
    """Load all past inference logs and plot the risk score trend."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return

    inf_root = OUT_ROOT / "inference-log"
    history  = []

    for log_file in sorted(inf_root.rglob("*.md")):
        try:
            text = log_file.read_text()
            m    = re.search(r"Geo Risk Score: ([\d.]+)/100", text)
            d    = log_file.stem
            if m and re.match(r"\d{4}-\d{2}-\d{2}", d):
                history.append({"date": d, "score": float(m.group(1))})
        except Exception:
            continue

    # Add today
    history.append({"date": TODAY_STR, "score": today_score})
    history = sorted(history, key=lambda x: x["date"])

    if len(history) < 2:
        return  # Not enough history yet

    dates  = [h["date"] for h in history]
    scores = [h["score"] for h in history]

    fig, ax = plt.subplots(figsize=(12, 4))
    fig.patch.set_facecolor("#0d1117")
    ax.set_facecolor("#161b22")

    ax.fill_between(range(len(dates)), scores, alpha=0.2, color="#f59e0b")
    ax.plot(range(len(dates)), scores, "o-", color="#f59e0b", linewidth=2, markersize=6)
    ax.axhline(20, color="#22c55e", linewidth=1, linestyle=":", alpha=0.5, label="Low threshold")
    ax.axhline(40, color="#facc15", linewidth=1, linestyle=":", alpha=0.5, label="Moderate threshold")
    ax.axhline(60, color="#ef4444", linewidth=1, linestyle=":", alpha=0.5, label="High threshold")
    ax.set_xticks(range(len(dates)))
    ax.set_xticklabels(dates, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("Geopolitical Risk Score")
    ax.set_title("Geopolitical Risk Score — Historical Trend (Options-Implied)",
                 color="white", fontsize=12)
    ax.set_ylim(0, 100)
    ax.legend(facecolor="#1f2937", labelcolor="white", fontsize=8)
    ax.grid(True, alpha=0.3, axis="y")
    for spine in ax.spines.values():
        spine.set_edgecolor("#374151")

    plt.tight_layout()
    p = CHART_DIR / "04_geo_risk_history.png"
    plt.savefig(p, dpi=140, bbox_inches="tight")
    plt.close()
    print(f"  Saved: {p.name}")

# ─────────────────────────────────────────────────────────────────────────────
# AGENT 4 — PERSISTENCE + REGIME CHANGE DETECTION
# ─────────────────────────────────────────────────────────────────────────────
def agent4_persist(spot: float, df: pd.DataFrame, insights: dict,
                   geopolitical_context: str):
    print("\n" + "="*65)
    print("AGENT 4 — PERSISTENCE + REGIME DETECTION")
    print("="*65)

    # ── Load yesterday's score for regime change detection ─────────────────
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    yesterday_log = OUT_ROOT / "inference-log" / yesterday[:7] / f"{yesterday}.md"
    prev_score = None
    if yesterday_log.exists():
        text = yesterday_log.read_text()
        m    = re.search(r"Geo Risk Score: ([\d.]+)/100", text)
        if m:
            prev_score = float(m.group(1))

    today_score = insights.get("geo_risk_score", 0)
    regime_delta = today_score - prev_score if prev_score is not None else None

    if regime_delta is not None:
        abs_delta = abs(regime_delta)
        if abs_delta >= 15:
            regime_flag = f"🚨 REGIME SHIFT: {regime_delta:+.1f} pts vs yesterday"
        elif abs_delta >= 7:
            regime_flag = f"⚠  Notable change: {regime_delta:+.1f} pts vs yesterday"
        else:
            regime_flag = f"Stable: {regime_delta:+.1f} pts vs yesterday"
    else:
        regime_flag = "First observation — no baseline yet"

    print(f"Today: {today_score:.1f}  |  Yesterday: {prev_score or 'N/A'}  |  {regime_flag}")

    # ── Write daily log ────────────────────────────────────────────────────
    ts_list = insights.get("term_structure", [])
    skew_list = insights.get("skew", [])

    log_lines = [
        f"# Vol Surface Inference — {TODAY_STR}",
        f"",
        f"**Spot**: {spot:,.2f}  |  **Run**: {datetime.now().strftime('%H:%M IST')}  |  **Context**: {geopolitical_context}",
        f"",
        f"## Geo Risk Score: {today_score:.1f}/100",
        f"",
        f"> {insights.get('risk_label', '')}",
        f"",
        f"**Regime change**: {regime_flag}",
        f"",
        f"**Score breakdown**:",
    ]
    for k, v in insights.get("score_breakdown", {}).items():
        log_lines.append(f"- {k.replace('_', ' ').title()}: {v}")

    log_lines += [
        f"",
        f"## Term Structure",
        f"",
        f"| Expiry | DTE | ATM IV |",
        f"|--------|-----|--------|",
    ]
    for t in ts_list:
        log_lines.append(f"| {t['expiry']} | {t['dte']} | {t['atm_iv']*100:.1f}% |")

    slope   = insights.get("term_slope", 0)
    inv_str = "**INVERTED**" if insights.get("ts_inverted") else "Normal"
    log_lines += [
        f"",
        f"Shape: {inv_str}  |  Slope (far–near): {slope*100:+.1f}pp",
        f"",
        f"## Skew (Risk Reversal)",
        f"",
        f"| Expiry | DTE | RR (pp) | Signal |",
        f"|--------|-----|---------|--------|",
    ]
    for s in sorted(skew_list, key=lambda x: x["dte"]):
        sig = "Put skew" if s["rr"] > 0.005 else "Call skew" if s["rr"] < -0.005 else "Balanced"
        log_lines.append(f"| {s['expiry']} | {s['dte']} | {s['rr']*100:+.1f} | {sig} |")

    log_lines += [
        f"",
        f"## Full Inference",
        f"",
        f"```",
        insights.get("inference_text", ""),
        f"```",
        f"",
        f"## Charts",
        f"",
        f"- [Vol Surface 3D](../../../charts/{TODAY_STR}/01_vol_surface_3d.png)",
        f"- [Term Structure](../../../charts/{TODAY_STR}/02_term_structure.png)",
        f"- [Skew & Smile](../../../charts/{TODAY_STR}/03_skew_and_smile.png)",
        f"- [Risk Score History](../../../charts/{TODAY_STR}/04_geo_risk_history.png)",
        f"",
        f"---",
        f"*Auto-generated by vol_surface_monitor.py*",
    ]

    LOG_FILE.write_text("\n".join(log_lines))
    print(f"Log saved: {LOG_FILE}")

    return regime_delta

# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Nifty Vol Surface Monitor")
    parser.add_argument("--context", default="Iran war risk",
                        help="Geopolitical context for inference (default: 'Iran war risk')")
    args = parser.parse_args()

    print(f"\nNIFTY VOL SURFACE MONITOR")
    print(f"Run: {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  Context: {args.context}")
    print("="*65)

    spot, expiries, chains = agent1_collect()
    df                     = agent2_build_surface(spot, expiries, chains)
    insights               = agent3_infer(spot, df, geopolitical_context=args.context)

    print("\n" + "="*65)
    print("GENERATING CHARTS")
    print("="*65)
    generate_charts(spot, df, insights)

    delta = agent4_persist(spot, df, insights, args.context)

    print("\n" + "="*65)
    print("COMPLETE")
    print("="*65)
    print(f"Charts:    {CHART_DIR}/")
    print(f"Log:       {LOG_FILE}")
    score = insights.get("geo_risk_score", 0)
    print(f"Score:     {score:.1f}/100  — {insights.get('risk_label', '')}")
    if delta is not None and abs(delta) >= 7:
        print(f"ALERT:     Score moved {delta:+.1f} pts since yesterday")
