"""
Normalized walk-forward engine models.

These models are intentionally small. They create stable boundaries for
strategies/providers without changing the existing SILVERMIC paper-trading
behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any


@dataclass(frozen=True)
class InstrumentRef:
    """Broker-neutral reference for the instrument being monitored."""

    instrument_key: str
    trading_symbol: str
    expiry: str = ""
    segment: str = ""
    underlying: str = ""


@dataclass(frozen=True)
class Candle:
    """Normalized OHLC candle consumed by strategy and position adapters."""

    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    oi: float = 0.0

    @classmethod
    def from_legacy(cls, candle: dict[str, Any]) -> "Candle":
        return cls(
            timestamp=candle["timestamp"],
            open=float(candle["open"]),
            high=float(candle["high"]),
            low=float(candle["low"]),
            close=float(candle["close"]),
            volume=float(candle.get("volume", 0.0) or 0.0),
            oi=float(candle.get("oi", 0.0) or 0.0),
        )

    def to_legacy(self) -> dict[str, Any]:
        """Return the dict shape used by the current detector/trade manager."""

        return {
            "timestamp": self.timestamp,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "oi": self.oi,
        }


@dataclass(frozen=True)
class Quote:
    """Normalized latest quote/LTP snapshot."""

    instrument_key: str
    ltp: float
    timestamp: datetime | None = None


@dataclass
class DayContext:
    """Session initialization context passed to the selected strategy."""

    instrument: InstrumentRef
    prev_day_ohlc: dict[str, Any]
    session_date: date
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunnerProfile:
    """Defines a specific validator instance configuration."""
    profile_id: str
    instrument_key_prefix: str
    strategy_id: str
    position_plan_id: str
    display_prefix: str = "WFV"
    runner_label: str = ""

    def with_overrides(
        self,
        strategy_id: str | None = None,
        position_plan_id: str | None = None,
    ) -> "RunnerProfile":
        return RunnerProfile(
            profile_id=self.profile_id,
            instrument_key_prefix=self.instrument_key_prefix,
            strategy_id=strategy_id or self.strategy_id,
            position_plan_id=position_plan_id or self.position_plan_id,
            display_prefix=self.display_prefix,
            runner_label=self.runner_label or self.profile_id,
        )
