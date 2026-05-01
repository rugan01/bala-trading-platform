"""
SILVERMIC V3 strategy adapter.

This class wraps the existing SignalDetector so strategy selection can become
configuration-driven without changing the actual V3 CPR Band TC/BC rules.
"""

from __future__ import annotations

from typing import Any

from cpr_calculator import CPRCalculator
from models import Candle, DayContext
from signal_detector import Signal, SignalDetector


class SilvermicCprBandV3Strategy:
    strategy_id = "silvermic_cpr_band_v3"
    display_name = "SILVERMIC V3 CPR Band TC/BC Rejection"

    def __init__(self):
        self.day_context: DayContext | None = None
        self.cpr: CPRCalculator | None = None
        self.detector: SignalDetector | None = None

    def initialize(self, day_context: DayContext, warmup_candles: list[Candle]) -> None:
        self.day_context = day_context
        self.cpr = CPRCalculator(day_context.prev_day_ohlc)
        self.detector = SignalDetector(
            self.cpr,
            [item.to_legacy() for item in warmup_candles],
        )

    def on_candle(self, candle: Candle) -> Signal | None:
        if self.detector is None:
            raise RuntimeError("Strategy has not been initialized")
        return self.detector.process_candle(candle.to_legacy())

    def get_state_snapshot(self) -> dict[str, Any]:
        if self.detector is None:
            return {}
        return {
            "strategy_id": self.strategy_id,
            "display_name": self.display_name,
            "trail_st": self.detector.get_trail_st_value(),
            "sl_st": self.detector.get_sl_st_value(),
            "cpr": self.cpr,
        }

    def get_day_start_context(self) -> CPRCalculator:
        if self.cpr is None:
            raise RuntimeError("Strategy CPR is not initialized")
        return self.cpr
