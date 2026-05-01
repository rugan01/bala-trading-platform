"""
SILVERMIC CPR breakout/continuation strategy.

This is intentionally simple and deterministic so it can act as a second
registered strategy for replay and walk-forward comparison work:

- Long when price closes through CPR TC from below
- Short when price closes through CPR BC from above
- Initial SL uses SuperTrend(5,3) when valid, else CPR breakout level +/- fallback
- T1 uses R1 for longs / S1 for shorts, with R2/S2 as the next extension if
  the entry is already beyond R1/S1

It is not meant to replace the existing V3 rejection logic. It gives the
engine a second distinct strategy profile for experimentation.
"""

from __future__ import annotations

import logging
from typing import Any

from config import Config
from cpr_calculator import CPRCalculator
from models import Candle, DayContext
from signal_detector import Signal
from supertrend import SuperTrend

logger = logging.getLogger(__name__)


class SilvermicCprBreakoutStrategy:
    strategy_id = "silvermic_cpr_breakout_v1"
    display_name = "SILVERMIC CPR Breakout Continuation"

    def __init__(self):
        self.day_context: DayContext | None = None
        self.cpr: CPRCalculator | None = None
        self.st_sl = SuperTrend(Config.ST_SL_LENGTH, Config.ST_SL_FACTOR)
        self.st_trail = SuperTrend(Config.ST_TRAIL_LENGTH, Config.ST_TRAIL_FACTOR)
        self.prev_close: float | None = None
        self.long_fired = False
        self.short_fired = False

    def initialize(self, day_context: DayContext, warmup_candles: list[Candle]) -> None:
        self.day_context = day_context
        self.cpr = CPRCalculator(day_context.prev_day_ohlc)
        legacy = [item.to_legacy() for item in warmup_candles]
        if legacy:
            self.st_sl.warmup(legacy)
            self.st_trail.warmup(legacy)
            self.prev_close = warmup_candles[-1].close
            logger.info(
                "[Breakout] Warmed up with %s bars | ST_SL=%s | ST_Trail=%s",
                len(warmup_candles),
                self.st_sl,
                self.st_trail,
            )
        else:
            logger.warning("[Breakout] No warm-up candles — SuperTrend needs first few live bars to stabilize")

    def on_candle(self, candle: Candle) -> Signal | None:
        if self.cpr is None:
            raise RuntimeError("Strategy has not been initialized")

        previous_close = self.prev_close
        self.st_sl.update(candle.to_legacy())
        self.st_trail.update(candle.to_legacy())

        signal = None
        if previous_close is not None:
            if self._is_long_breakout(previous_close, candle):
                signal = self._build_signal("Long", candle, self.cpr.tc)
                self.long_fired = True
            elif self._is_short_breakout(previous_close, candle):
                signal = self._build_signal("Short", candle, self.cpr.bc)
                self.short_fired = True

        self.prev_close = candle.close
        return signal

    def _is_long_breakout(self, previous_close: float, candle: Candle) -> bool:
        if self.long_fired or self.cpr is None:
            return False
        return (
            previous_close <= self.cpr.tc
            and candle.close > self.cpr.tc
            and candle.close > candle.open
            and candle.high >= self.cpr.tc
        )

    def _is_short_breakout(self, previous_close: float, candle: Candle) -> bool:
        if self.short_fired or self.cpr is None:
            return False
        return (
            previous_close >= self.cpr.bc
            and candle.close < self.cpr.bc
            and candle.close < candle.open
            and candle.low <= self.cpr.bc
        )

    def _build_signal(self, direction: str, candle: Candle, breakout_level: float) -> Signal:
        if self.cpr is None:
            raise RuntimeError("Strategy CPR is not initialized")

        is_long = direction == "Long"
        sl, sl_source = self._compute_sl(direction, candle.close, breakout_level)
        if is_long:
            t1 = self.cpr.r1 if candle.close < self.cpr.r1 else self.cpr.levels.r2
        else:
            t1 = self.cpr.s1 if candle.close > self.cpr.s1 else self.cpr.levels.s2

        logger.info(
            "[Breakout] SIGNAL %s | Entry=%.2f | Level=%.2f | SL=%.2f (%s) | T1=%.2f",
            direction,
            candle.close,
            breakout_level,
            sl,
            sl_source,
            t1,
        )

        return Signal(
            direction=direction,
            entry_price=candle.close,
            sl=sl,
            t1=t1,
            bc=self.cpr.bc,
            tc=self.cpr.tc,
            pivot=self.cpr.pivot,
            timestamp=candle.timestamp,
            touch_level=breakout_level,
            sl_source=sl_source,
            bar_index=0,
        )

    def _compute_sl(self, direction: str, entry_price: float, breakout_level: float) -> tuple[float, str]:
        is_long = direction == "Long"
        if self.st_sl.is_ready() and self.st_sl.value is not None:
            st_val = self.st_sl.value
            if is_long and st_val < entry_price:
                return float(st_val), "SuperTrend"
            if not is_long and st_val > entry_price:
                return float(st_val), "SuperTrend"

        if is_long:
            return breakout_level * (1.0 - Config.SL_FALLBACK_PCT), "Fallback"
        return breakout_level * (1.0 + Config.SL_FALLBACK_PCT), "Fallback"

    def get_state_snapshot(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "display_name": self.display_name,
            "trail_st": self.st_trail.value,
            "sl_st": self.st_sl.value,
            "cpr": self.cpr,
            "prev_close": self.prev_close,
        }

