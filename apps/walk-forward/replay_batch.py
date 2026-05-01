"""
Batch replay runner for walk-forward research experiments.

This stays paper/replay-only. It runs many replay configurations, writes each
individual replay artifact, then creates one ranked batch summary that is easy
to inspect in a spreadsheet or hand to another LLM.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytz

from config import Config
from replay import execute_replay
from replay_provider import available_session_dates, load_candles_csv
from position_plans import registered_position_plan_ids
from strategy_registry import registered_strategy_ids

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)

EXPECTED_REPLAY_ARGS = {
    "csv",
    "self_test",
    "session_date",
    "warmup_bars",
    "strategy_id",
    "position_plan_id",
    "instrument_key",
    "trading_symbol",
    "expiry",
    "segment",
    "underlying",
    "prev_date",
    "prev_open",
    "prev_high",
    "prev_low",
    "prev_close",
    "output_dir",
    "run_id",
    "no_save",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run many replay experiments and write a ranked summary.")
    parser.add_argument("--manifest", default=None, help="JSON manifest with defaults and runs.")
    parser.add_argument("--csv", default=None, help="Single candle CSV to replay once or across many session dates.")
    parser.add_argument("--csv-dir", default=None, help="Directory of candle CSV files to replay.")
    parser.add_argument("--pattern", default="*.csv", help="CSV glob pattern for --csv-dir.")
    parser.add_argument("--self-test", action="store_true", help="Run one deterministic batch smoke test.")
    parser.add_argument("--session-date", default=None, help="Optional single session date for CSV replay.")
    parser.add_argument("--all-session-dates", action="store_true", help="Run every replayable session date found in --csv or each file in --csv-dir.")
    parser.add_argument("--date-from", default=None, help="Optional lower date bound (YYYY-MM-DD) for --all-session-dates.")
    parser.add_argument("--date-to", default=None, help="Optional upper date bound (YYYY-MM-DD) for --all-session-dates.")
    parser.add_argument("--strategy-id", default=Config.WFV_STRATEGY_ID)
    parser.add_argument("--strategy-ids", nargs="*", default=None, help="Optional list of strategy ids to run as a matrix.")
    parser.add_argument("--position-plan-id", default=Config.WFV_POSITION_PLAN_ID)
    parser.add_argument("--position-plan-ids", nargs="*", default=None, help="Optional list of position plan ids to run as a matrix.")
    parser.add_argument("--warmup-bars", type=int, default=Config.ST_WARMUP_BARS)
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parent / "output" / "replay_batch"),
        help="Directory where batch artifacts will be written.",
    )
    parser.add_argument("--batch-id", default=None, help="Optional batch folder name.")
    parser.add_argument("--no-save", action="store_true", help="Print summary only; do not write artifacts.")
    parser.add_argument("--list-strategies", action="store_true", help="Print registered strategy ids and exit.")
    parser.add_argument("--list-position-plans", action="store_true", help="Print registered position plan ids and exit.")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    try:
        args = parse_args()
        if args.list_strategies:
            print("Registered strategies")
            for item in registered_strategy_ids():
                print(f"- {item}")
            return 0
        if args.list_position_plans:
            print("Registered position plans")
            for item in registered_position_plan_ids():
                print(f"- {item}")
            return 0
        runs = build_batch_runs(args)
        if not runs:
            raise ValueError("No runs found. Use --self-test, --manifest, --csv, or --csv-dir.")

        batch_id = args.batch_id or datetime.now(IST).strftime("batch_%Y%m%d_%H%M%S")
        batch_dir = Path(args.output_dir).expanduser() / sanitize_id(batch_id)
        run_output_dir = batch_dir / "runs"

        rows: list[dict[str, Any]] = []
        reports: list[dict[str, Any]] = []
        for idx, run in enumerate(runs, start=1):
            run_id = run.get("run_id") or f"run_{idx:03d}"
            replay_args = to_replay_namespace(
                {
                    **run,
                    "run_id": sanitize_id(run_id),
                    "output_dir": str(run_output_dir),
                    "no_save": args.no_save,
                }
            )
            logger.info("Batch run %s/%s starting | run_id=%s", idx, len(runs), replay_args.run_id)
            try:
                output = execute_replay(replay_args)
                row = summary_row(output.report, output.artifacts)
                rows.append(row)
                reports.append(output.report)
            except Exception as exc:
                logger.error("Batch run failed | run_id=%s | error=%s", replay_args.run_id, exc)
                rows.append(failed_summary_row(replay_args.run_id, replay_args, exc))

        ranked = rank_rows(rows)
        print_batch_summary(ranked)

        if not args.no_save:
            write_batch_artifacts(batch_dir, ranked, reports)
            print(f"- batch_summary_csv: {batch_dir / 'batch_summary.csv'}")
            print(f"- batch_summary_json: {batch_dir / 'batch_summary.json'}")
        return 0
    except Exception as exc:
        logger.error("Replay batch failed: %s", exc)
        return 1


def build_batch_runs(args: argparse.Namespace) -> list[dict[str, Any]]:
    strategy_ids = args.strategy_ids or [args.strategy_id]
    position_plan_ids = args.position_plan_ids or [args.position_plan_id]

    if args.self_test:
        base = replay_defaults(args) | {"self_test": True, "csv": None, "trading_symbol": "SILVERMIC REPLAY"}
        return matrix_runs(base, ["self_test"], strategy_ids, position_plan_ids, run_id_prefix="self_test")

    if args.manifest:
        return runs_from_manifest(Path(args.manifest).expanduser(), replay_defaults(args))

    if args.csv:
        return runs_from_csv(
            Path(args.csv).expanduser(),
            replay_defaults(args),
            strategy_ids,
            position_plan_ids,
            session_date=args.session_date,
            all_session_dates=args.all_session_dates,
            date_from=parse_optional_date(args.date_from),
            date_to=parse_optional_date(args.date_to),
        )

    if args.csv_dir:
        return runs_from_csv_dir(
            Path(args.csv_dir).expanduser(),
            args.pattern,
            replay_defaults(args),
            strategy_ids,
            position_plan_ids,
            session_date=args.session_date,
            all_session_dates=args.all_session_dates,
            date_from=parse_optional_date(args.date_from),
            date_to=parse_optional_date(args.date_to),
        )

    return []


def replay_defaults(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "csv": None,
        "self_test": False,
        "session_date": args.session_date,
        "warmup_bars": args.warmup_bars,
        "strategy_id": args.strategy_id,
        "position_plan_id": args.position_plan_id,
        "instrument_key": "REPLAY|SILVERMIC",
        "trading_symbol": "SILVERMIC REPLAY",
        "expiry": "",
        "segment": "MCX_FO",
        "underlying": "SILVERMIC",
        "prev_date": None,
        "prev_open": None,
        "prev_high": None,
        "prev_low": None,
        "prev_close": None,
    }


def runs_from_manifest(path: Path, base_defaults: dict[str, Any]) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    defaults = base_defaults | raw.get("defaults", {})
    runs = raw.get("runs", [])
    if not isinstance(runs, list):
        raise ValueError("Manifest 'runs' must be a list")
    return [normalize_manifest_run(defaults | run, idx, path.parent) for idx, run in enumerate(runs, start=1)]


def normalize_manifest_run(run: dict[str, Any], idx: int, manifest_dir: Path) -> dict[str, Any]:
    normalized = normalize_run(run, idx)
    csv_path = normalized.get("csv")
    if csv_path and not Path(csv_path).expanduser().is_absolute():
        normalized["csv"] = str((manifest_dir / csv_path).resolve())
    return normalized


def runs_from_csv_dir(
    csv_dir: Path,
    pattern: str,
    defaults: dict[str, Any],
    strategy_ids: list[str],
    position_plan_ids: list[str],
    session_date: str | None,
    all_session_dates: bool,
    date_from: date | None,
    date_to: date | None,
) -> list[dict[str, Any]]:
    if not csv_dir.exists():
        raise ValueError(f"CSV directory does not exist: {csv_dir}")
    runs = []
    for path in sorted(csv_dir.glob(pattern)):
        if not path.is_file():
            continue
        runs.extend(
            runs_from_csv(
                path,
                defaults | {"trading_symbol": f"SILVERMIC REPLAY {path.stem}"},
                strategy_ids,
                position_plan_ids,
                session_date=session_date,
                all_session_dates=all_session_dates,
                date_from=date_from,
                date_to=date_to,
                run_id_prefix=path.stem,
            )
        )
    return runs


def runs_from_csv(
    csv_path: Path,
    defaults: dict[str, Any],
    strategy_ids: list[str],
    position_plan_ids: list[str],
    session_date: str | None,
    all_session_dates: bool,
    date_from: date | None,
    date_to: date | None,
    run_id_prefix: str | None = None,
) -> list[dict[str, Any]]:
    candles = load_candles_csv(csv_path)
    if all_session_dates:
        session_dates = available_session_dates(candles, date_from=date_from, date_to=date_to)
        if not session_dates:
            raise ValueError(f"No replayable session dates found in {csv_path}")
        session_labels = [item.isoformat() for item in session_dates]
    else:
        chosen = session_date or candles[-1].timestamp.astimezone(IST).date().isoformat()
        session_labels = [chosen]

    base = defaults | {
        "csv": str(csv_path),
        "self_test": False,
    }
    return matrix_runs(
        base,
        session_labels,
        strategy_ids,
        position_plan_ids,
        run_id_prefix=run_id_prefix or csv_path.stem,
    )


def normalize_run(run: dict[str, Any], idx: int) -> dict[str, Any]:
    normalized = dict(run)
    normalized["run_id"] = sanitize_id(str(normalized.get("run_id") or f"run_{idx:03d}"))
    for key in ["prev_open", "prev_high", "prev_low", "prev_close"]:
        if normalized.get(key) is not None:
            normalized[key] = float(normalized[key])
    if normalized.get("warmup_bars") is not None:
        normalized["warmup_bars"] = int(normalized["warmup_bars"])
    return normalized


def matrix_runs(
    base_run: dict[str, Any],
    session_dates: list[str],
    strategy_ids: list[str],
    position_plan_ids: list[str],
    run_id_prefix: str,
) -> list[dict[str, Any]]:
    runs: list[dict[str, Any]] = []
    idx = 0
    for session_date in session_dates:
        for strategy_id in strategy_ids:
            for position_plan_id in position_plan_ids:
                idx += 1
                run = base_run | {
                    "session_date": session_date if session_date != "self_test" else None,
                    "strategy_id": strategy_id,
                    "position_plan_id": position_plan_id,
                    "run_id": f"{run_id_prefix}__{session_date}__{strategy_id}__{position_plan_id}",
                }
                runs.append(normalize_run(run, idx))
    return runs


def parse_optional_date(value: str | None) -> date | None:
    return date.fromisoformat(value) if value else None


def to_replay_namespace(run: dict[str, Any]) -> argparse.Namespace:
    args = {key: run.get(key) for key in EXPECTED_REPLAY_ARGS}
    return argparse.Namespace(**args)


def summary_row(report: dict[str, Any], artifacts) -> dict[str, Any]:
    summary = report["summary"]
    config = report["run_config"]
    instrument = config.get("instrument", {})
    return {
        "status": "ok",
        "error": "",
        "run_id": artifacts.json_path.stem if artifacts else config.get("run_id", ""),
        "strategy_id": config.get("strategy_id"),
        "position_plan_id": config.get("position_plan_id"),
        "session_date": config.get("session_date"),
        "trading_symbol": instrument.get("trading_symbol"),
        "csv": config.get("csv"),
        "closed_trades": summary["closed_trades"],
        "entries_taken": summary["entries_taken"],
        "win_rate_pct": summary["win_rate_pct"],
        "net_pnl": summary["net_pnl"],
        "gross_pnl": summary["gross_pnl"],
        "profit_factor": summary["profit_factor"],
        "expectancy": summary["expectancy"],
        "average_r": summary["average_r"],
        "total_r": summary["total_r"],
        "max_drawdown": summary["max_drawdown"],
        "max_consecutive_losses": summary["max_consecutive_losses"],
        "report_json": str(artifacts.json_path) if artifacts else "",
        "trades_csv": str(artifacts.trades_csv_path) if artifacts else "",
    }


def failed_summary_row(run_id: str, replay_args: argparse.Namespace, exc: Exception) -> dict[str, Any]:
    return {
        "status": "failed",
        "error": str(exc),
        "run_id": run_id,
        "strategy_id": replay_args.strategy_id,
        "position_plan_id": replay_args.position_plan_id,
        "session_date": replay_args.session_date,
        "trading_symbol": replay_args.trading_symbol,
        "csv": replay_args.csv,
        "closed_trades": 0,
        "entries_taken": 0,
        "win_rate_pct": 0.0,
        "net_pnl": 0.0,
        "gross_pnl": 0.0,
        "profit_factor": None,
        "expectancy": 0.0,
        "average_r": 0.0,
        "total_r": 0.0,
        "max_drawdown": 0.0,
        "max_consecutive_losses": 0,
        "report_json": "",
        "trades_csv": "",
    }


def rank_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = sorted(
        rows,
        key=lambda row: (
            1 if row.get("status") == "ok" else 0,
            float(row.get("net_pnl") or 0.0),
            float(row.get("average_r") or 0.0),
            -float(row.get("max_drawdown") or 0.0),
        ),
        reverse=True,
    )
    for idx, row in enumerate(ranked, start=1):
        row["rank"] = idx
    return ranked


def print_batch_summary(rows: list[dict[str, Any]]) -> None:
    print("Replay batch summary")
    print(f"- runs: {len(rows)}")
    for row in rows[:10]:
        if row.get("status") == "failed":
            print(f"- #{row['rank']} {row['run_id']} | FAILED | {row['error']}")
        else:
            print(
                f"- #{row['rank']} {row['run_id']} | trades={row['closed_trades']} "
                f"net={float(row['net_pnl']):.2f} avgR={float(row['average_r']):.2f} "
                f"dd={float(row['max_drawdown']):.2f}"
            )


def write_batch_artifacts(batch_dir: Path, rows: list[dict[str, Any]], reports: list[dict[str, Any]]) -> None:
    batch_dir.mkdir(parents=True, exist_ok=True)
    summary_json = {
        "generated_at": datetime.now(IST).isoformat(),
        "runs": rows,
        "reports": reports,
    }
    (batch_dir / "batch_summary.json").write_text(json.dumps(summary_json, indent=2, sort_keys=True), encoding="utf-8")
    write_rows_csv(rows, batch_dir / "batch_summary.csv")


def write_rows_csv(rows: list[dict[str, Any]], path: Path) -> None:
    if not rows:
        return
    fieldnames = [
        "rank",
        "status",
        "error",
        "run_id",
        "strategy_id",
        "position_plan_id",
        "session_date",
        "trading_symbol",
        "csv",
        "closed_trades",
        "entries_taken",
        "win_rate_pct",
        "net_pnl",
        "gross_pnl",
        "profit_factor",
        "expectancy",
        "average_r",
        "total_r",
        "max_drawdown",
        "max_consecutive_losses",
        "report_json",
        "trades_csv",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def sanitize_id(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return text.strip("._-") or "run"


if __name__ == "__main__":
    sys.exit(main())
