"""
Replay result metrics and persistence.

Artifacts are intentionally plain JSON/CSV so another LLM, spreadsheet, or
future dashboard can consume them without depending on the runtime internals.
"""

from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import pytz

IST = pytz.timezone("Asia/Kolkata")


@dataclass(frozen=True)
class ReplayArtifacts:
    json_path: Path
    trades_csv_path: Path


def build_replay_report(result, run_config: dict[str, Any]) -> dict[str, Any]:
    trades = [serialize_trade(trade) for trade in result.closed_trades]
    summary = compute_summary(trades, result)
    return {
        "generated_at": datetime.now(IST).isoformat(),
        "run_config": run_config,
        "summary": summary,
        "trades": trades,
    }


def compute_summary(trades: list[dict[str, Any]], result) -> dict[str, Any]:
    net_values = [float(trade["net_pnl"] or 0.0) for trade in trades]
    gross_values = [float(trade["gross_pnl"] or 0.0) for trade in trades]
    r_values = [float(trade["r_multiple"] or 0.0) for trade in trades if trade["r_multiple"] is not None]
    wins = [value for value in net_values if value > 0]
    losses = [value for value in net_values if value < 0]
    breakeven = [value for value in net_values if value == 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    return {
        "candles_processed": result.candles_processed,
        "signals_seen": result.signals_seen,
        "entries_taken": result.entries_taken,
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate_pct": round((len(wins) / len(trades)) * 100, 2) if trades else 0.0,
        "gross_pnl": round(sum(gross_values), 2),
        "net_pnl": round(sum(net_values), 2),
        "average_net_pnl": round(sum(net_values) / len(net_values), 2) if net_values else 0.0,
        "average_win": round(sum(wins) / len(wins), 2) if wins else 0.0,
        "average_loss": round(sum(losses) / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "expectancy": round(sum(net_values) / len(net_values), 2) if net_values else 0.0,
        "average_r": round(sum(r_values) / len(r_values), 2) if r_values else 0.0,
        "total_r": round(sum(r_values), 2) if r_values else 0.0,
        "max_drawdown": round(max_drawdown(net_values), 2),
        "max_consecutive_losses": max_consecutive_losses(net_values),
    }


def serialize_trade(trade) -> dict[str, Any]:
    exit_reason = trade.exit_reason.value if trade.exit_reason else None
    return {
        "trade_num": trade.trade_num,
        "label": trade.label,
        "trading_symbol": trade.trading_symbol,
        "direction": trade.direction,
        "entry_time": iso_or_none(trade.entry_time),
        "entry_price": trade.entry_price,
        "sl": trade.sl,
        "t1": trade.t1,
        "t1_exit_time": iso_or_none(trade.t1_exit_time),
        "t1_exit_price": trade.t1_exit_price,
        "exit_time": iso_or_none(trade.exit_time),
        "exit_price": trade.exit_price,
        "exit_reason": exit_reason,
        "lots_total": trade.lots_total,
        "t1_exit_lots": trade.t1_exit_lots(),
        "lots_open_after_t1": trade.lots_open,
        "position_plan": asdict(trade.position_plan),
        "gross_pnl": trade.gross_pnl(),
        "net_pnl": trade.net_pnl(),
        "r_multiple": trade.r_multiple(),
        "outcome": trade.outcome(),
        "signal": {
            "bar_index": trade.signal.bar_index,
            "touch_level": trade.signal.touch_level,
            "sl_source": trade.signal.sl_source,
            "bc": trade.signal.bc,
            "tc": trade.signal.tc,
            "pivot": trade.signal.pivot,
            "timestamp": iso_or_none(trade.signal.timestamp),
        },
    }


def write_replay_artifacts(report: dict[str, Any], output_dir: Path, run_id: str | None = None) -> ReplayArtifacts:
    output_dir.mkdir(parents=True, exist_ok=True)
    artifact_id = run_id or datetime.now(IST).strftime("replay_%Y%m%d_%H%M%S")
    json_path = output_dir / f"{artifact_id}.json"
    trades_csv_path = output_dir / f"{artifact_id}_trades.csv"

    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    write_trades_csv(report["trades"], trades_csv_path)
    return ReplayArtifacts(json_path=json_path, trades_csv_path=trades_csv_path)


def write_trades_csv(trades: list[dict[str, Any]], path: Path) -> None:
    fieldnames = [
        "trade_num",
        "label",
        "trading_symbol",
        "direction",
        "entry_time",
        "entry_price",
        "sl",
        "t1",
        "t1_exit_time",
        "t1_exit_price",
        "exit_time",
        "exit_price",
        "exit_reason",
        "lots_total",
        "t1_exit_lots",
        "gross_pnl",
        "net_pnl",
        "r_multiple",
        "outcome",
        "position_plan_id",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for trade in trades:
            writer.writerow(
                {
                    **{field: trade.get(field) for field in fieldnames if field != "position_plan_id"},
                    "position_plan_id": trade.get("position_plan", {}).get("plan_id"),
                }
            )


def max_drawdown(pnl_values: list[float]) -> float:
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in pnl_values:
        equity += value
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return max_dd


def max_consecutive_losses(pnl_values: list[float]) -> int:
    current = 0
    max_seen = 0
    for value in pnl_values:
        if value < 0:
            current += 1
            max_seen = max(max_seen, current)
        else:
            current = 0
    return max_seen


def iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value else None
