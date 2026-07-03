#!/usr/bin/env python3
"""Generate a transparent sector-rotation and stock-selection scorecard."""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_SRC = REPO_ROOT / "packages" / "trading_platform" / "src"
for path in (str(REPO_ROOT), str(PACKAGE_SRC), str(REPO_ROOT / "apps" / "briefing")):
    if path not in sys.path:
        sys.path.insert(0, path)

from fno_scanner import FNO_UNIVERSE, get_upstox_provider  # noqa: E402
from trading_platform.stock_selection.scoring import (  # noqa: E402
    build_snapshot,
    build_stock_scorecards,
    load_fundamentals_csv,
)
from trading_platform.stock_selection.universe import current_fno_stock_symbols  # noqa: E402


DEFAULT_OUTPUT_DIR = Path(
    os.getenv(
        "STOCK_SELECTION_OUTPUT_DIR",
        "/Users/rugan/balas-product-os/Projects/trading-system/stock-selection/reports",
    )
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fundamentals", help="Optional fundamentals CSV")
    parser.add_argument("--symbols", help="Comma-separated subset of the F&O universe")
    parser.add_argument("--top", type=int, default=20, help="Number of stocks in markdown summary")
    parser.add_argument("--workers", type=int, default=5, help="Parallel Upstox history requests")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR))
    return parser.parse_args()


def sector_by_symbol() -> dict[str, str]:
    return {
        symbol: sector
        for sector, symbols in FNO_UNIVERSE.items()
        for symbol in symbols
    }


def candle_values(payload: dict[str, Any]) -> tuple[list[float], float | None]:
    if not payload:
        return [], None

    # UpstoxDataProvider exposes parallel OHLCV arrays. Keep the candle-object
    # fallback so the scorer remains usable with other market-data adapters.
    closes = [float(value) for value in payload.get("closes", []) if value is not None]
    volumes = [float(value) for value in payload.get("volumes", []) if value is not None]

    if not closes:
        candles = payload.get("candles", [])
        closes = [
            float(candle["close"])
            for candle in candles
            if isinstance(candle, dict) and candle.get("close") is not None
        ]
        volumes = [
            float(candle["volume"])
            for candle in candles
            if isinstance(candle, dict) and candle.get("volume") is not None
        ]

    paired_periods = min(21, len(closes), len(volumes))
    turnovers = [
        close * volume
        for close, volume in zip(closes[-paired_periods:], volumes[-paired_periods:])
    ]
    return closes, sum(turnovers) / len(turnovers) if turnovers else None


def fetch_snapshots(symbols: list[str], workers: int) -> tuple[list[Any], list[str]]:
    provider = get_upstox_provider()
    benchmark_payload = provider.get_market_data("NIFTY", period="1y", mode="eod", kind="index")
    benchmark_closes, _ = candle_values(benchmark_payload)
    if len(benchmark_closes) < 127:
        raise RuntimeError("NIFTY history is insufficient for six-month scoring")

    sectors = sector_by_symbol()
    snapshots: list[Any] = []
    errors: list[str] = []

    def fetch(symbol: str) -> Any:
        payload = provider.get_market_data(symbol, period="1y", mode="eod", kind="equity")
        closes, turnover = candle_values(payload)
        if len(closes) < 127:
            raise RuntimeError(f"only {len(closes)} daily candles")
        return build_snapshot(symbol, sectors.get(symbol, "Other"), closes, benchmark_closes, turnover)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(fetch, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                snapshots.append(future.result())
            except Exception as exc:
                errors.append(f"{symbol}: {exc}")
    return snapshots, errors


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def upsert_history(
    path: Path,
    rows: list[dict[str, Any]],
    key_fields: tuple[str, ...],
) -> None:
    """Keep one score row per date/key for StockEdge-style trend charts."""
    existing: list[dict[str, Any]] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as handle:
            existing = list(csv.DictReader(handle))

    keyed = {
        tuple(str(row.get(field, "")) for field in key_fields): dict(row)
        for row in existing
    }
    for row in rows:
        keyed[tuple(str(row.get(field, "")) for field in key_fields)] = dict(row)

    ordered = sorted(
        keyed.values(),
        key=lambda row: tuple(str(row.get(field, "")) for field in key_fields),
    )
    write_csv(path, ordered)


def flatten_stock(card: Any) -> dict[str, Any]:
    row = card.to_dict()
    row["momentum_1m"] = card.momentum_scores["1m"]
    row["momentum_3m"] = card.momentum_scores["3m"]
    row["momentum_6m"] = card.momentum_scores["6m"]
    row["reasons"] = "; ".join(card.reasons)
    row["red_flags"] = "; ".join(card.red_flags)
    for group, score in card.fundamental_groups.items():
        row[f"fundamental_{group}"] = score
    row.pop("momentum_scores", None)
    row.pop("fundamental_groups", None)
    return row


def markdown_report(stock_cards: list[Any], sectors: list[Any], errors: list[str], top: int) -> str:
    generated = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")
    lines = [
        "# Stock Selection Scorecard",
        "",
        f"Generated: {generated}",
        "",
        "This is a transparent StockEdge-like model. It does not reproduce StockEdge's proprietary formula.",
        "",
        "## Sector Rotation",
        "",
        "| Sector | Score | 1M | 3M | 6M | Breadth | Stocks |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for sector in sectors:
        lines.append(
            f"| {sector.sector} | {sector.sector_score:.1f} | {sector.momentum_1m:.1f} | "
            f"{sector.momentum_3m:.1f} | {sector.momentum_6m:.1f} | "
            f"{sector.breadth_score:.1f} | {sector.stock_count} |"
        )
    lines.extend(
        [
            "",
            "## Ranked Stocks",
            "",
            "| Stock | Sector | Status | Final | Momentum | Fundamental | 1M | 3M | 6M |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for card in stock_cards[:top]:
        fundamental = (
            f"{card.fundamental_score:.1f}" if card.fundamental_score is not None else "Required"
        )
        lines.append(
            f"| {card.symbol} | {card.sector} | {card.status} | {card.final_score:.1f} | "
            f"{card.momentum_score:.1f} | {fundamental} | {card.momentum_scores['1m']:.1f} | "
            f"{card.momentum_scores['3m']:.1f} | {card.momentum_scores['6m']:.1f} |"
        )
    lines.extend(
        [
            "",
            "## Selection Rules",
            "",
            "- `ELIGIBLE`: sector, stock momentum, and fundamentals are each at least 60, with no red flags.",
            "- `RESEARCH_REQUIRED`: technically promising, but fundamentals have not been supplied.",
            "- `WATCH`: promising composite, but one or more gates are below 60.",
            "- `AVOID`: weak composite or a fundamental red flag.",
            "",
            "Momentum uses cross-sectional return, relative strength versus NIFTY, trend, proximity to highs, and a volatility penalty. Sector scores combine constituent momentum with breadth above key moving averages.",
        ]
    )
    if errors:
        lines.extend(["", "## Data Errors", ""])
        lines.extend(f"- {error}" for error in errors)
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    requested = (
        [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
        if args.symbols
        else current_fno_stock_symbols()
    )
    print(f"Technical universe: {len(requested)} symbols")
    output_dir = Path(args.output_dir).expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshots, errors = fetch_snapshots(requested, args.workers)
    if not snapshots:
        raise RuntimeError("No stock histories could be scored")
    fundamentals = load_fundamentals_csv(args.fundamentals)
    stocks, sectors = build_stock_scorecards(snapshots, fundamentals)
    stamp = date.today().isoformat()

    stock_rows = [flatten_stock(card) for card in stocks]
    sector_rows = [sector.to_dict() for sector in sectors]
    write_csv(output_dir / f"{stamp}_stock_scores.csv", stock_rows)
    write_csv(output_dir / f"{stamp}_sector_scores.csv", sector_rows)
    upsert_history(
        output_dir / "stock_score_history.csv",
        [
            {
                "score_date": stamp,
                "symbol": row.get("symbol"),
                "sector": row.get("sector"),
                "status": row.get("status"),
                "final_score": row.get("final_score"),
                "momentum_score": row.get("momentum_score"),
                "fundamental_score": row.get("fundamental_score"),
                "sector_score": row.get("sector_score"),
                "momentum_1m": row.get("momentum_1m"),
                "momentum_3m": row.get("momentum_3m"),
                "momentum_6m": row.get("momentum_6m"),
            }
            for row in stock_rows
        ],
        ("score_date", "symbol"),
    )
    upsert_history(
        output_dir / "sector_score_history.csv",
        [{"score_date": stamp, **dict(row)} for row in sector_rows],
        ("score_date", "sector"),
    )
    (output_dir / f"{stamp}_scorecard.json").write_text(
        json.dumps(
            {
                "generated_at": datetime.now().astimezone().isoformat(),
                "stocks": stock_rows,
                "sectors": sector_rows,
                "errors": errors,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    report = markdown_report(stocks, sectors, errors, args.top)
    report_path = output_dir / f"{stamp}_stock_selection.md"
    report_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"\nSaved: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
