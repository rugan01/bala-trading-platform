"""
Replay runner for the walk-forward engine.

This is a paper-only research tool. It runs the same strategy and position plan
over historical/synthetic candles without live broker APIs.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pytz

from config import Config
from event_loop import CandleEventLoop
from models import Candle, DayContext, InstrumentRef, RunnerProfile
from paper_position_manager import PaperPositionManager
from position_plans import create_position_plan
from replay_provider import ReplayDataProvider, infer_prev_day_ohlc, load_candles_csv, split_replay_candles
from replay_results import ReplayArtifacts, build_replay_report, write_replay_artifacts
from runtime import MockNotionLogger, MockTelegramAlerter
from strategy_registry import create_strategy
from trade_manager import TradeManager

IST = pytz.timezone("Asia/Kolkata")
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplayRunOutput:
    report: dict
    artifacts: ReplayArtifacts | None = None


def build_replay_provider(args: argparse.Namespace) -> ReplayDataProvider:
    instrument = InstrumentRef(
        instrument_key=args.instrument_key,
        trading_symbol=args.trading_symbol,
        expiry=args.expiry,
        segment=args.segment,
        underlying=args.underlying,
    )

    if args.self_test:
        prev_day_ohlc, warmup, replay = build_self_test_candles()
        return ReplayDataProvider(instrument, prev_day_ohlc, warmup, replay)

    if not args.csv:
        raise ValueError("Provide --csv or use --self-test")

    candles = load_candles_csv(Path(args.csv))
    session_date = date.fromisoformat(args.session_date) if args.session_date else candles[-1].timestamp.astimezone(IST).date()
    prev_day_ohlc = explicit_prev_day_ohlc(args) or infer_prev_day_ohlc(candles, session_date)
    warmup, replay = split_replay_candles(candles, session_date, args.warmup_bars)
    return ReplayDataProvider(instrument, prev_day_ohlc, warmup, replay)


def explicit_prev_day_ohlc(args: argparse.Namespace) -> dict | None:
    values = [args.prev_open, args.prev_high, args.prev_low, args.prev_close]
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError("Provide all of --prev-open/--prev-high/--prev-low/--prev-close, or none")
    return {
        "date": args.prev_date or "manual",
        "open": float(args.prev_open),
        "high": float(args.prev_high),
        "low": float(args.prev_low),
        "close": float(args.prev_close),
    }


def build_self_test_candles() -> tuple[dict, list[Candle], list[Candle]]:
    """
    Build deterministic candles that trigger a long signal, hit T1, then force
    close the remaining lot. This validates strategy + position-plan wiring.
    """

    prev_day_ohlc = {"date": "2026-04-16", "open": 100.0, "high": 110.0, "low": 90.0, "close": 105.0}
    warmup_start = IST.localize(datetime(2026, 4, 16, 9, 15))
    warmup = [
        Candle(
            timestamp=warmup_start + timedelta(minutes=15 * idx),
            open=100.0 + (idx % 3) * 0.1,
            high=101.0 + (idx % 3) * 0.1,
            low=99.0 - (idx % 2) * 0.1,
            close=100.4 + (idx % 3) * 0.1,
            volume=1000,
        )
        for idx in range(Config.ST_WARMUP_BARS)
    ]

    base = IST.localize(datetime(2026, 4, 17, 17, 0))
    replay = [
        Candle(base + timedelta(minutes=0), 101.0, 101.4, 100.0, 101.0, 1000),
        Candle(base + timedelta(minutes=15), 101.0, 102.0, 100.8, 101.5, 1000),
        Candle(base + timedelta(minutes=30), 101.5, 102.1, 100.7, 101.8, 1000),
        Candle(base + timedelta(minutes=45), 101.8, 102.2, 100.0, 101.2, 1000),
        Candle(base + timedelta(minutes=60), 101.2, 104.2, 101.1, 103.8, 1000),
        Candle(base + timedelta(minutes=75), 103.8, 104.0, 102.5, 103.0, 1000),
    ]
    return prev_day_ohlc, warmup, replay


def execute_replay(args: argparse.Namespace) -> ReplayRunOutput:
    provider = build_replay_provider(args)
    instrument = provider.resolve_instrument()

    profile = RunnerProfile(
        profile_id=args.run_id or "replay_run",
        instrument_key_prefix=args.underlying,
        strategy_id=args.strategy_id or Config.WFV_STRATEGY_ID,
        position_plan_id=args.position_plan_id or Config.WFV_POSITION_PLAN_ID,
        display_prefix="REPLAY"
    )

    strategy = create_strategy(profile.strategy_id)
    position_plan = create_position_plan(profile.position_plan_id)

    journal = MockNotionLogger()
    alerts = MockTelegramAlerter()
    position_manager = PaperPositionManager(
        TradeManager(
            notion_logger=journal,
            telegram_alerter=alerts,
            trading_symbol=instrument.trading_symbol,
            position_plan=position_plan,
        )
    )

    warmup = provider.get_warmup_candles(instrument, args.warmup_bars)
    replay = provider.get_intraday_candles(instrument)
    day_context = DayContext(
        instrument=instrument,
        prev_day_ohlc=provider.get_prev_day_ohlc(instrument),
        session_date=replay[0].timestamp.astimezone(IST).date(),
        metadata={"mode": "replay"},
    )
    strategy.initialize(day_context, warmup)

    logger.info(
        "Replay starting | strategy=%s | position_plan=%s | candles=%s | warmup=%s",
        strategy.strategy_id,
        position_plan.plan_id,
        len(replay),
        len(warmup),
    )

    loop = CandleEventLoop(strategy, position_manager)
    result = loop.process_many(replay)
    if position_manager.has_open_position():
        last = replay[-1]
        loop.force_close_open_position(last.close, last.timestamp)

    run_config = build_run_config(args, provider, strategy.strategy_id, position_plan.plan_id, day_context.session_date)
    report = build_replay_report(result, run_config)
    artifacts = None

    if not args.no_save:
        artifacts = write_replay_artifacts(
            report,
            Path(args.output_dir).expanduser(),
            run_id=args.run_id,
        )
    return ReplayRunOutput(report=report, artifacts=artifacts)


def run_replay(args: argparse.Namespace) -> int:
    output = execute_replay(args)
    print_summary(output.report)
    if output.artifacts:
        print(f"- report_json: {output.artifacts.json_path}")
        print(f"- trades_csv: {output.artifacts.trades_csv_path}")
    return 0


def build_run_config(
    args: argparse.Namespace,
    provider: ReplayDataProvider,
    strategy_id: str,
    position_plan_id: str,
    session_date: date,
) -> dict:
    return {
        "mode": "self_test" if args.self_test else "csv",
        "run_id": args.run_id,
        "csv": args.csv,
        "session_date": session_date.isoformat(),
        "strategy_id": strategy_id,
        "position_plan_id": position_plan_id,
        "instrument": {
            "instrument_key": provider.instrument.instrument_key,
            "trading_symbol": provider.instrument.trading_symbol,
            "expiry": provider.instrument.expiry,
            "segment": provider.instrument.segment,
            "underlying": provider.instrument.underlying,
        },
        "prev_day_ohlc": provider.prev_day_ohlc,
        "warmup_bars": len(provider.warmup_candles),
        "replay_candles": len(provider.replay_candles),
    }


def print_summary(report: dict) -> None:
    summary = report["summary"]
    print("Replay summary")
    print(f"- candles processed: {summary['candles_processed']}")
    print(f"- signals seen: {summary['signals_seen']}")
    print(f"- entries taken: {summary['entries_taken']}")
    print(f"- closed trades: {summary['closed_trades']}")
    print(f"- win rate: {summary['win_rate_pct']:.2f}%")
    print(f"- gross pnl: {summary['gross_pnl']:.2f}")
    print(f"- net pnl: {summary['net_pnl']:.2f}")
    print(f"- profit factor: {summary['profit_factor']}")
    print(f"- average R: {summary['average_r']:.2f}")
    print(f"- max drawdown: {summary['max_drawdown']:.2f}")
    print(f"- max consecutive losses: {summary['max_consecutive_losses']}")
    for trade in report["trades"]:
        reason = trade["exit_reason"] or "-"
        print(
            f"- trade #{trade['trade_num']}: {trade['direction']} {trade['trading_symbol']} "
            f"entry={trade['entry_price']:.2f} exit={trade['exit_price']:.2f} "
            f"net={trade['net_pnl']:.2f} reason={reason}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay walk-forward strategy over historical/synthetic candles.")
    parser.add_argument("--csv", default=None, help="CSV with timestamp,open,high,low,close[,volume,oi].")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic built-in replay smoke test.")
    parser.add_argument("--session-date", default=None, help="Replay session date in YYYY-MM-DD. Defaults to latest CSV candle date.")
    parser.add_argument("--warmup-bars", type=int, default=Config.ST_WARMUP_BARS)
    parser.add_argument("--strategy-id", default=None)
    parser.add_argument("--position-plan-id", default=None)
    parser.add_argument("--instrument-key", default="REPLAY|SILVERMIC")
    parser.add_argument("--trading-symbol", default="SILVERMIC REPLAY")
    parser.add_argument("--expiry", default="")
    parser.add_argument("--segment", default="MCX_FO")
    parser.add_argument("--underlying", default="SILVERMIC")
    parser.add_argument("--prev-date", default=None)
    parser.add_argument("--prev-open", type=float, default=None)
    parser.add_argument("--prev-high", type=float, default=None)
    parser.add_argument("--prev-low", type=float, default=None)
    parser.add_argument("--prev-close", type=float, default=None)
    parser.add_argument(
        "--output-dir",
        default=str(Path(__file__).resolve().parent / "output" / "replay"),
        help="Directory for replay JSON/CSV artifacts.",
    )
    parser.add_argument("--run-id", default=None, help="Optional artifact filename prefix.")
    parser.add_argument("--no-save", action="store_true", help="Print summary only; do not write JSON/CSV artifacts.")
    return parser.parse_args()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    try:
        return run_replay(parse_args())
    except Exception as exc:
        logger.error("Replay failed: %s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
