"""
Protocol interfaces for the walk-forward runtime.

The current engine remains paper-only. These protocols define the seam where
future strategies, market-data providers, and sinks can plug in safely.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from models import Candle, DayContext, InstrumentRef, Quote
from signal_detector import Signal


class MarketDataProvider(Protocol):
    def resolve_instrument(self) -> InstrumentRef:
        ...

    def get_prev_day_ohlc(self, instrument: InstrumentRef) -> dict[str, Any]:
        ...

    def get_warmup_candles(self, instrument: InstrumentRef, n: int) -> list[Candle]:
        ...

    def get_intraday_candles(self, instrument: InstrumentRef) -> list[Candle]:
        ...

    def get_latest_quote(self, instrument: InstrumentRef) -> Quote | None:
        ...


class Strategy(Protocol):
    strategy_id: str
    display_name: str

    def initialize(self, day_context: DayContext, warmup_candles: list[Candle]) -> None:
        ...

    def on_candle(self, candle: Candle) -> Signal | None:
        ...

    def get_state_snapshot(self) -> dict[str, Any]:
        ...


class PositionManager(Protocol):
    def can_enter(self) -> bool:
        ...

    def enter(self, signal: Signal, timestamp: datetime) -> None:
        ...

    def update(self, candle: Candle, strategy_state: dict[str, Any] | None = None) -> None:
        ...

    def force_close_all(self, price: float, timestamp: datetime) -> None:
        ...

    def has_open_position(self) -> bool:
        ...

    def pop_new_closed_trades(self, seen_count: int) -> tuple[list[Any], int]:
        ...


class JournalSink(Protocol):
    def create_entry(self, trade) -> str | None:
        ...

    def update_exit(self, trade) -> bool:
        ...


class AlertSink(Protocol):
    def send_day_start(self, context: Any) -> None:
        ...

    def send_signal(self, trade) -> None:
        ...

    def send_t1_hit(self, trade, t1_pnl: float) -> None:
        ...

    def send_trade_closed(self, trade) -> None:
        ...

    def send_day_summary(self, trades: list, date_str: str) -> None:
        ...

    def send_error(self, message: str) -> None:
        ...
