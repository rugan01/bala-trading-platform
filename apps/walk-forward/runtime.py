"""
Runtime composition for the walk-forward validator.

This is deliberately a small composition layer, not a framework. It lets the
runner select strategy/provider components while preserving paper-only behavior.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from config import Config  # Still needed for global API keys
from interfaces import AlertSink, JournalSink, MarketDataProvider, PositionManager, Strategy
from notion_logger import NotionLogger
from paper_position_manager import PaperPositionManager
from position_plans import PositionPlan, create_position_plan
from strategy_registry import create_strategy
from telegram_alerts import TelegramAlerter
from trade_manager import TradeManager
from upstox_provider import UpstoxMarketDataProvider
from models import RunnerProfile

logger = logging.getLogger(__name__)


@dataclass
class RuntimeBundle:
    data_provider: MarketDataProvider
    strategy: Strategy
    journal_sink: JournalSink
    alert_sink: AlertSink
    position_plan: PositionPlan
    position_manager_factory: Callable[[str], PositionManager]


class MockNotionLogger:
    def create_entry(self, trade) -> str:
        logger.info(f"[DRY RUN] Notion create: {trade.label}")
        return "mock-page-id"

    def update_exit(self, trade) -> bool:
        net = trade.net_pnl() or 0
        logger.info(f"[DRY RUN] Notion update: {trade.label} | Net P&L=₹{net:.0f}")
        return True


class MockTelegramAlerter:
    def __init__(self, runner_label: str = "Walk-Forward"):
        self.runner_label = runner_label

    def send_day_start(self, context):
        logger.info(f"[DRY RUN] Telegram [{self.runner_label}]: Day start | {context.summary()}")

    def send_signal(self, trade):
        logger.info(f"[DRY RUN] Telegram [{self.runner_label}]: Signal {trade.direction} #{trade.trade_num}")

    def send_t1_hit(self, trade, t1_pnl):
        logger.info(f"[DRY RUN] Telegram [{self.runner_label}]: T1 hit #{trade.trade_num} | P&L=₹{t1_pnl:.0f}")

    def send_trade_closed(self, trade):
        logger.info(f"[DRY RUN] Telegram [{self.runner_label}]: Closed #{trade.trade_num} | {trade.outcome()}")

    def send_day_summary(self, trades, date_str):
        logger.info(f"[DRY RUN] Telegram [{self.runner_label}]: Day summary | {len(trades)} trades")

    def send_error(self, message):
        logger.info(f"[DRY RUN] Telegram error: {message}")


def build_runtime(
    profile: RunnerProfile,
    dry_run: bool = False,
    include_all_intraday_candles: bool = False,
) -> RuntimeBundle:
    strategy = create_strategy(profile.strategy_id)
    position_plan = create_position_plan(profile.position_plan_id)
    journal_sink: JournalSink = MockNotionLogger() if dry_run else NotionLogger()
    alert_sink: AlertSink = MockTelegramAlerter(profile.runner_label) if dry_run else TelegramAlerter(profile.runner_label)
    data_provider: MarketDataProvider = UpstoxMarketDataProvider(
        include_all_intraday_candles=include_all_intraday_candles,
        instrument_prefix=profile.instrument_key_prefix,
    )

    def build_position_manager(trading_symbol: str) -> PositionManager:
        trade_manager = TradeManager(
            notion_logger=journal_sink,
            telegram_alerter=alert_sink,
            trading_symbol=trading_symbol,
            position_plan=position_plan,
            profile_id=profile.profile_id,
            trade_label_prefix=profile.display_prefix,
            strategy_display_name=strategy.display_name,
        )
        return PaperPositionManager(trade_manager)

    logger.info(
        "Runtime selected [%s] | strategy=%s | plan=%s | dry_run=%s",
        profile.profile_id,
        profile.strategy_id,
        profile.position_plan_id,
        dry_run,
    )

    return RuntimeBundle(
        data_provider=data_provider,
        strategy=strategy,
        journal_sink=journal_sink,
        alert_sink=alert_sink,
        position_plan=position_plan,
        position_manager_factory=build_position_manager,
    )
