#!/usr/bin/env python3
"""Build scorecard-ready historical fundamentals from cached Screener pages."""

from __future__ import annotations

import argparse
import csv
from datetime import date
import os
from pathlib import Path
import sys
import time

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SRC = REPO_ROOT / "packages" / "trading_platform" / "src"
sys.path.insert(0, str(PACKAGE_SRC))

from trading_platform.stock_selection.scoring import score_fundamentals  # noqa: E402
from trading_platform.stock_selection.screener import (  # noqa: E402
    SCREENER_BASE_URL,
    parse_screener_html,
)
from trading_platform.stock_selection.universe import current_fno_stock_symbols  # noqa: E402


DEFAULT_OUTPUT_DIR = Path(
    "/Users/rugan/balas-product-os/Projects/trading-system/stock-selection"
)


def _write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row})
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _fetch(symbol: str, cache_path: Path) -> str:
    response = requests.get(
        SCREENER_BASE_URL.format(symbol=symbol),
        headers={"User-Agent": "Bala personal research scorecard/1.0"},
        timeout=30,
    )
    response.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(response.text, encoding="utf-8")
    return response.text


def _fmt(value: object) -> str:
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "-"


def _metric_delta(
    history: list[dict], symbol: str, metric: str, lookback: int = 3
) -> float | None:
    rows = [row for row in history if row.get("symbol") == symbol and row.get(metric) is not None]
    if len(rows) <= lookback:
        return None
    try:
        return float(rows[-1][metric]) - float(rows[-1 - lookback][metric])
    except (TypeError, ValueError):
        return None


def _trend_diagnosis(row: dict, history: list[dict]) -> str:
    symbol = str(row["symbol"])
    positives: list[str] = []
    cautions: list[str] = []

    margin_delta = _metric_delta(history, symbol, "ebitda_margin")
    roce_delta = _metric_delta(history, symbol, "roce")
    debt_delta = _metric_delta(history, symbol, "debt_to_equity")

    if margin_delta is not None:
        (positives if margin_delta >= 2 else cautions if margin_delta <= -2 else []).append(
            f"operating margin {'expanded' if margin_delta >= 2 else 'contracted'} "
            f"{abs(margin_delta):.1f}pp in 3 years"
        )
    if roce_delta is not None:
        (positives if roce_delta >= 3 else cautions if roce_delta <= -3 else []).append(
            f"ROCE {'improved' if roce_delta >= 3 else 'fell'} "
            f"{abs(roce_delta):.1f}pp in 3 years"
        )
    if debt_delta is not None:
        (positives if debt_delta <= -0.15 else cautions if debt_delta >= 0.15 else []).append(
            f"debt/equity {'reduced' if debt_delta <= -0.15 else 'rose'} "
            f"{abs(debt_delta):.2f} in 3 years"
        )

    cfo_to_pat = row.get("cfo_to_pat")
    fcf_margin = row.get("fcf_margin")
    pe = row.get("pe")
    sales_cagr = row.get("sales_cagr_3y")
    profit_yoy = row.get("profit_yoy")
    if cfo_to_pat is not None:
        if float(cfo_to_pat) >= 0.9:
            positives.append("profits are supported by operating cash flow")
        elif float(cfo_to_pat) < 0.6:
            cautions.append("weak cash conversion versus reported profit")
    if fcf_margin is not None and float(fcf_margin) < 0:
        cautions.append("latest free cash flow is negative")
    if pe is not None and float(pe) >= 60:
        cautions.append(f"high current PE of {float(pe):.0f}")
    if sales_cagr is not None and profit_yoy is not None:
        if float(sales_cagr) < 8 and float(profit_yoy) > 40:
            cautions.append("latest profit jump looks like a rebound, not broad multi-year growth")

    summary = []
    if positives:
        summary.append("Strengths: " + "; ".join(positives[:3]) + ".")
    if cautions:
        summary.append("Watch: " + "; ".join(cautions[:3]) + ".")
    return " ".join(summary) or "No decisive multi-year change detected from the available fields."


def _analysis(rows: list[dict], history: list[dict], errors: list[dict], output: Path) -> None:
    scored = []
    for row in rows:
        result = score_fundamentals(row)
        scored.append((result.overall or 0.0, result.coverage, row))
    scored.sort(key=lambda item: item[0], reverse=True)

    lines = [
        f"# Screener Fundamental Analysis - {date.today().isoformat()}",
        "",
        "Cache-first personal research prototype. Screener does not provide an official API; "
        "use its supported export workflow for production-scale updates.",
        "",
        "| Rank | Stock | Fundamental score | Coverage | Sales YoY | Profit YoY | 3Y sales CAGR | ROCE | Debt/equity |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, (score, coverage, row) in enumerate(scored, 1):
        lines.append(
            f"| {rank} | {row['symbol']} | {score:.1f} | {coverage:.0%} | "
            f"{_fmt(row.get('sales_yoy'))}% | {_fmt(row.get('profit_yoy'))}% | "
            f"{_fmt(row.get('sales_cagr_3y'))}% | {_fmt(row.get('roce'))}% | "
            f"{_fmt(row.get('debt_to_equity'))} |"
        )
    lines.extend(["", "## Historical Trend Diagnosis", ""])
    for score, _, row in scored:
        lines.append(
            f"- **{row['symbol']} ({score:.1f}/100):** {_trend_diagnosis(row, history)}"
        )
    lines.extend(
        [
            "",
            "## Coverage Notes",
            "",
            "- Historical growth, margins, cash conversion, leverage, ROCE and shareholding are calculated from annual Screener tables.",
            "- Current PE, price/book, dividend yield, ROE and ROCE come from the current company-page ratio cards.",
            "- Current ratio, quick ratio, ROA, industry PE, PEG, EV/EBITDA, price/free-cash-flow and promoter pledge require a custom Screener export or another source.",
            "- Current valuation ratios are deliberately excluded from older history rows to prevent look-ahead bias.",
            "- Operating profit is used as an EBITDA proxy because that is the comparable annual row exposed on public Screener pages.",
            "- Banks and other financial companies require a separate sector-specific model; forcing them into industrial-company leverage and margin ratios would be misleading.",
        ]
    )
    if errors:
        lines.extend(
            [
                "",
                "## Skipped Symbols",
                "",
                "| Symbol | Reason |",
                "|---|---|",
            ]
        )
        for error in errors:
            lines.append(f"| {error['symbol']} | {error['error']} |")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--symbols",
        help="Comma-separated NSE symbols; defaults to current NSE stock-F&O underlyings",
    )
    parser.add_argument("--fetch", action="store_true", help="Fetch missing cache pages")
    parser.add_argument("--delay", type=float, default=2.0)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    cache_dir = args.cache_dir or args.output_dir / "screener-cache"
    latest_rows: list[dict] = []
    history_rows: list[dict] = []
    errors: list[dict] = []
    symbols = (
        [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
        if args.symbols
        else current_fno_stock_symbols()
    )
    print(f"Fundamental universe: {len(symbols)} symbols")

    for index, symbol in enumerate(symbols):
        try:
            cache_path = cache_dir / f"{symbol}.html"
            if cache_path.exists():
                html = cache_path.read_text(encoding="utf-8")
            elif args.fetch:
                html = _fetch(symbol, cache_path)
                if index < len(symbols) - 1:
                    time.sleep(args.delay)
            else:
                raise FileNotFoundError(
                    f"Missing {cache_path}; rerun with --fetch or add a cached/exported page"
                )
            latest, history = parse_screener_html(symbol, html)
            latest_rows.append(latest)
            history_rows.extend(history)
        except (FileNotFoundError, requests.RequestException, ValueError) as exc:
            errors.append({"symbol": symbol, "error": str(exc).replace("|", "/")})

    if latest_rows:
        _write_csv(args.output_dir / "fundamentals.csv", latest_rows)
    if history_rows:
        _write_csv(args.output_dir / "fundamental-history.csv", history_rows)
    error_path = args.output_dir / "screener-import-errors.csv"
    if errors:
        _write_csv(error_path, errors)
    elif error_path.exists():
        error_path.unlink()
    report = (
        args.output_dir / "reports" / f"{date.today().isoformat()}_screener_fundamental_analysis.md"
    )
    _analysis(latest_rows, history_rows, errors, report)
    print(
        f"Wrote {len(latest_rows)} latest rows, {len(history_rows)} history rows, "
        f"skipped {len(errors)} symbols and wrote {report}"
    )


if __name__ == "__main__":
    main()
