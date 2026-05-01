"""
Paper position-manager adapter.

The existing TradeManager remains the source of truth for trade lifecycle. This
adapter only exposes it through a smaller PositionManager interface.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from models import Candle
from signal_detector import Signal
from trade_manager import TradeManager


class PaperPositionManager:
    """Adapter around TradeManager for paper-only execution."""

    execution_mode = "paper"

    def __init__(self, trade_manager: TradeManager):
        self.trade_manager = trade_manager

    def can_enter(self) -> bool:
        return (
            not self.trade_manager.has_open_trade()
            and not self.trade_manager.is_daily_limit_reached()
        )

    def enter(self, signal: Signal, timestamp: datetime) -> None:
        self.trade_manager.enter_trade(signal, timestamp)

    def update(self, candle: Candle, strategy_state: dict[str, Any] | None = None) -> None:
        trail_st = (strategy_state or {}).get("trail_st")
        self.trade_manager.update_trade(candle.to_legacy(), trail_st)

    def force_close_all(self, price: float, timestamp: datetime) -> None:
        self.trade_manager.force_close_all(price, timestamp)

    def has_open_position(self) -> bool:
        return self.trade_manager.has_open_trade()

    def pop_new_closed_trades(self, seen_count: int) -> tuple[list[Any], int]:
        return self.trade_manager.pop_new_closed_trades(seen_count)
