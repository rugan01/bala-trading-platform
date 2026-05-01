"""
Reusable candle event loop for replay and future live runners.

This keeps the candle-processing sequence in one small unit:
- strategy sees the closed candle
- open positions are managed first
- new signals enter only when no position is open
- force close can be applied at the configured EOD time
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from config import Config
from interfaces import PositionManager, Strategy
from models import Candle

logger = logging.getLogger(__name__)


@dataclass
class CandleEventLoopResult:
    candles_processed: int = 0
    signals_seen: int = 0
    entries_taken: int = 0
    closed_trades: list[Any] = field(default_factory=list)


class CandleEventLoop:
    """
    Process normalized closed candles using a strategy and position manager.

    The default sequencing mirrors the current live paper runner. This is the
    reference event loop for replay/backtest parity work.
    """

    def __init__(
        self,
        strategy: Strategy,
        position_manager: PositionManager,
        force_close_hour: int = Config.FORCE_CLOSE_H,
        force_close_minute: int = Config.FORCE_CLOSE_M,
    ):
        self.strategy = strategy
        self.position_manager = position_manager
        self.force_close_hour = force_close_hour
        self.force_close_minute = force_close_minute
        self.result = CandleEventLoopResult()
        self._closed_trade_seen_count = 0
        self._last_processed_ts: datetime | None = None

    def process_candle(self, candle: Candle) -> None:
        if self._last_processed_ts and candle.timestamp <= self._last_processed_ts:
            logger.debug("[EventLoop] Skipping duplicate/out-of-order candle: %s", candle.timestamp)
            return

        self._last_processed_ts = candle.timestamp
        self.result.candles_processed += 1

        logger.info(
            "[EventLoop] Candle @ %s | O=%s H=%s L=%s C=%s",
            candle.timestamp.strftime("%H:%M"),
            candle.open,
            candle.high,
            candle.low,
            candle.close,
        )

        if self._is_force_close_time(candle.timestamp):
            if self.position_manager.has_open_position():
                self.position_manager.force_close_all(candle.close, candle.timestamp)
                self._capture_closed_trades()
            return

        signal = self.strategy.on_candle(candle)
        if signal:
            self.result.signals_seen += 1

        if self.position_manager.has_open_position():
            self.position_manager.update(candle, self.strategy.get_state_snapshot())
            if not self.position_manager.has_open_position():
                self._capture_closed_trades()
        elif signal:
            if self.position_manager.can_enter():
                self.position_manager.enter(signal, candle.timestamp)
                self.result.entries_taken += 1
            else:
                logger.info("[EventLoop] Signal skipped because position manager cannot enter")

    def process_many(self, candles: list[Candle]) -> CandleEventLoopResult:
        for candle in sorted(candles, key=lambda item: item.timestamp):
            self.process_candle(candle)
        return self.result

    def force_close_open_position(self, price: float, timestamp: datetime) -> None:
        if self.position_manager.has_open_position():
            self.position_manager.force_close_all(price, timestamp)
            self._capture_closed_trades()

    def _capture_closed_trades(self) -> None:
        new_trades, self._closed_trade_seen_count = self.position_manager.pop_new_closed_trades(
            self._closed_trade_seen_count
        )
        self.result.closed_trades.extend(new_trades)

    def _is_force_close_time(self, timestamp: datetime) -> bool:
        current = (timestamp.hour, timestamp.minute)
        force_close = (self.force_close_hour, self.force_close_minute)
        return current >= force_close
